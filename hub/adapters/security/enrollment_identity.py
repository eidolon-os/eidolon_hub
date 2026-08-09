"""P-256 proof-of-possession verification for device enrollments."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hub.contracts.bindings.onboarding import DeviceEnrollment

_CONTEXT = b"eidolon-device-enrollment-proof-v1\n"


def _base64url_decode(value: str) -> bytes:
    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise PermissionError("invalid device identity proof encoding")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PermissionError("invalid device identity proof encoding") from exc


def _manifest_revision(enrollment: DeviceEnrollment) -> str:
    canonical = json.dumps(
        enrollment.manifest.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def enrollment_proof_statement(enrollment: DeviceEnrollment) -> bytes:
    """Return the unambiguous, language-neutral enrollment statement."""

    pairing = enrollment.pairing_proof
    values = (
        enrollment.request_id,
        enrollment.identity.device_id,
        "sha256:" + hashlib.sha256(enrollment.retrieval_token.encode()).hexdigest(),
        pairing.method if pairing else "",
        pairing.commitment if pairing else "",
        enrollment.device_kind,
        enrollment.display_name,
        _manifest_revision(enrollment),
    )
    framed = bytearray(_CONTEXT)
    for value in values:
        encoded = value.encode()
        framed.extend(str(len(encoded)).encode())
        framed.extend(b":")
        framed.extend(encoded)
        framed.extend(b"\n")
    return bytes(framed)


class P256EnrollmentIdentityVerifier:
    """Verify a detached ECDSA proof and return the stable SPKI fingerprint."""

    def verify(self, enrollment: DeviceEnrollment) -> str:
        proof = enrollment.identity_proof
        if proof is None:
            if enrollment.pairing_proof is not None:
                raise PermissionError("pairing enrollment requires device identity proof")
            return ""
        public_der = _base64url_decode(proof.public_key_spki)
        signature = _base64url_decode(proof.signature)
        try:
            public_key = serialization.load_der_public_key(public_der)
        except ValueError as exc:
            raise PermissionError("invalid device public key") from exc
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise PermissionError("device identity key must be P-256")
        try:
            public_key.verify(
                signature,
                enrollment_proof_statement(enrollment),
                ec.ECDSA(hashes.SHA256()),
            )
        except InvalidSignature as exc:
            raise PermissionError("invalid device identity proof") from exc
        canonical_der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return "p256:" + hashlib.sha256(canonical_der).hexdigest()
