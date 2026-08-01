"""P-256 proof verification for connection enrollment challenges."""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key

from hub.ports.identity import EnrollmentChallenge


def canonical_connection_proof(challenge: EnrollmentChallenge) -> bytes:
    return "\n".join(
        (
            "eidolon.connection.proof.v1",
            challenge.challenge_id,
            challenge.device_id,
            challenge.client_nonce,
            challenge.server_nonce,
        )
    ).encode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


class P256ConnectionProofVerifier:
    async def verify(
        self, *, challenge: EnrollmentChallenge, public_key: str, signature: str
    ) -> str:
        try:
            key = load_der_public_key(_decode(public_key))
        except Exception as exc:
            raise PermissionError("invalid connection public key") from exc
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name not in {
            "secp256r1",
            "prime256v1",
        }:
            raise PermissionError("connection public key must use P-256")
        try:
            key.verify(
                _decode(signature), canonical_connection_proof(challenge), ec.ECDSA(hashes.SHA256())
            )
        except (InvalidSignature, ValueError) as exc:
            raise PermissionError("invalid connection challenge signature") from exc
        return "p256:" + hashlib.sha256(_decode(public_key)).hexdigest()


class HmacLeaseCredentialIssuer:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("lease credential secret must contain at least 32 bytes")
        self._secret = secret

    def issue_lease_token(self, *, connection_id: str, device_id: str) -> str:
        digest = hmac.new(
            self._secret,
            f"eidolon.lease.v1\n{connection_id}\n{device_id}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
