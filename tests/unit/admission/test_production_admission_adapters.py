from __future__ import annotations

import base64
import hashlib

import jwt
import pytest
import rfc8785
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from eidolon_sdk.device_foundation.v1 import derive_device_instance_id
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from starlette.requests import Request

from hub.admission.auth import JwtAdmissionActorProvider
from hub.admission.commissioning import (
    commissioning_voucher_claims,
    derive_voucher_signing_key,
    sign_commissioning_voucher,
)
from hub.admission.domain import AdmissionProblem
from hub.composition.resources import RuntimeSecrets, load_commissioning_proof_verifier
from hub.config import CommissioningProofConfig, HubConfig

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_INSTANCE_01 = named_device_instance_id("device-instance-01")
_DEVICE_01 = named_device_instance_id("device_01")


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _spki(key: ec.EllipticCurvePrivateKey) -> tuple[str, bytes]:
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "p256-spki:" + base64.urlsafe_b64encode(der).rstrip(b"=").decode(), der


def _signature(key: ec.EllipticCurvePrivateKey, document: dict) -> str:
    der = key.sign(rfc8785.dumps(document), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return (
        base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        .rstrip(b"=")
        .decode()
    )


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


async def test_admission_actor_is_verified_from_short_lived_credential() -> None:
    secret = b"admission-owner-secret-value-000001"
    claims = {
        "sub": "eidolon-admin/admission-consumer",
        "presenter": "eidolon-admin/admission-consumer",
        "aud": "eidolon-admission",
        "actor": {
            "principal_id": "controller_01",
            "owner_domain_id": "owner-domain_01",
            "granted_scopes": ["device.read", "device.claim.approve"],
            "authentication_strength": "software",
        },
        "owner_domain_id": "owner-domain_01",
        "business_owner_id": "owner_01",
        "scopes": ["device.read", "device.claim.approve"],
        "exp": 4_000_000_000,
    }
    token = jwt.encode(claims, secret, algorithm="HS256")
    context = await JwtAdmissionActorProvider(secret=secret)(_request(token))
    assert context.actor.principal_id == "controller_01"
    assert str(context.business_owner_id) == "owner_01"

    crossed = jwt.encode(
        {**claims, "owner_domain_id": "owner-domain_02"}, secret, algorithm="HS256"
    )
    with pytest.raises(AdmissionProblem) as rejected:
        await JwtAdmissionActorProvider(secret=secret)(_request(crossed))
    assert rejected.value.code == "UNAUTHENTICATED"


def test_a_disabled_hub_fails_closed_rather_than_trusting_an_opaque_string() -> None:
    """The canonical route may be live before a Host is configured to sign.

    A Hub in that state must refuse, not improvise: an admission chain that
    accepts anything while it is "not configured yet" is the one nobody
    remembers to close later.
    """

    verifier, ready = load_commissioning_proof_verifier(
        HubConfig(commissioning_proof=CommissioningProofConfig(enabled=False)),
        RuntimeSecrets(management_jwt=b"m" * 32, device_registry_reader_token="r" * 32),
    )
    assert ready is False
    assert (
        verifier.verify(
            device_instance_id=_DEVICE_01,
            owner_domain_id="owner-domain_01",
            nonce="nonce_01",
            proof="opaque-proof",
            hardware_identity_evidence={
                "scheme": "hub-issued-base-p256",
                "evidence": "opaque",
            },
            operational_public_key="p256-spki:opaque",
            now_unix=1_700_000_000,
        )
        is None
    )


def test_configured_hub_verifies_its_own_voucher_and_refuses_another_key() -> None:
    """The signing key is derived, never installed, so the two sides cannot drift.

    What used to be here installed a root-owned per-device registry file and
    pinned its format in a constant — a gate added after a rollback across that
    format left the Hub restarting 110 times. The file is gone; this is what
    replaced it.
    """

    secrets = RuntimeSecrets(management_jwt=b"m" * 32, device_registry_reader_token="r" * 32)
    verifier, ready = load_commissioning_proof_verifier(HubConfig(), secrets)
    assert ready is True

    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    public_key, der = _spki(operational)
    device_instance_id = derive_device_instance_id(public_key)
    device_base_id = "device-base-" + "c3" * 32
    evidence_document = {
        "device_base_id": device_base_id,
        "device_instance_id": device_instance_id,
        "operational_public_key": public_key,
        "profile_id": "eidolon-trust-p256-hpke-v1",
    }
    evidence = (
        rfc8785.dumps(evidence_document).decode()
        + "."
        + _signature(operational, evidence_document)
    )
    claims = commissioning_voucher_claims(
        device_base_id=device_base_id,
        owner_domain_id="owner-domain_01",
        operational_spki_sha256="sha256:" + hashlib.sha256(der).hexdigest(),
        jti="jti-" + "0" * 32,
        expires_at_unix=4_102_444_800,
    )

    def voucher_for(payload: dict) -> str:
        """Sign whatever claims a case wants, through the canonical framing.

        Takes a payload rather than building one, because the refusals below
        are well-formed vouchers with one value changed — a different
        operational key, an expiry in the past — and must be framed exactly as
        a real one is, or they would be refused for the wrong reason.
        """

        return sign_commissioning_voucher(
            claims=payload,
            signing_key=derive_voucher_signing_key(secrets.management_jwt),
        )

    verified = verifier.verify(
        device_instance_id=device_instance_id,
        owner_domain_id="owner-domain_01",
        nonce=claims["jti"],
        proof=voucher_for(claims),
        hardware_identity_evidence={
            "scheme": "hub-issued-base-p256",
            "evidence": evidence,
            "evidence_digest": "sha256:" + hashlib.sha256(evidence.encode()).hexdigest(),
        },
        operational_public_key=public_key,
        now_unix=1_700_000_000,
    )
    assert verified is not None
    assert verified.identity.device_base_id == device_base_id
    assert verified.jti == claims["jti"]

    # The binding to one operational key is the whole of the voucher's value.
    other = ec.derive_private_key(0x456789123, ec.SECP256R1())
    _other_public, other_der = _spki(other)
    assert (
        verifier.verify(
            device_instance_id=device_instance_id,
            owner_domain_id="owner-domain_01",
            nonce=claims["jti"],
            proof=voucher_for(
                {**claims, "operational_spki_sha256": "sha256:" + hashlib.sha256(other_der).hexdigest()}
            ),
            hardware_identity_evidence={
                "scheme": "hub-issued-base-p256",
                "evidence": evidence,
                "evidence_digest": "sha256:" + hashlib.sha256(evidence.encode()).hexdigest(),
            },
            operational_public_key=public_key,
            now_unix=1_700_000_000,
        )
        is None
    )

    # An expired voucher is not a slow voucher.
    assert (
        verifier.verify(
            device_instance_id=device_instance_id,
            owner_domain_id="owner-domain_01",
            nonce=claims["jti"],
            proof=voucher_for({**claims, "exp": 1_600_000_000}),
            hardware_identity_evidence={
                "scheme": "hub-issued-base-p256",
                "evidence": evidence,
                "evidence_digest": "sha256:" + hashlib.sha256(evidence.encode()).hexdigest(),
            },
            operational_public_key=public_key,
            now_unix=1_700_000_000,
        )
        is None
    )


async def test_every_claim_this_surface_requires_is_required_by_name() -> None:
    """The Admission credential's claim set, pinned from the reader's side.

    Admin holds the other half. The two were allowed to drift once already: the
    removal path minted the Management surface's vocabulary — ``actor_ref`` as a
    bare string, ``owner_id`` instead of ``owner_domain_id``, no
    ``business_owner_id`` — and presented it here. This reader took
    ``claims["actor"]``, raised KeyError inside its own
    ``except (JWTError, KeyError, TypeError, ValueError)``, and answered 401
    with "invalid Admission credential". Every device removal ever attempted
    was refused by that, and nothing in the refusal mentioned a credential.

    Dropping any one of these has to be refused, or a half-shaped credential
    could be accepted and the drift would go unnoticed in the other direction.
    """

    secret = b"admission-owner-secret-value-000001"
    required = {
        "sub": "eidolon-admin/lifecycle-workflow",
        "presenter": "eidolon-admin/lifecycle-workflow",
        "aud": "eidolon-admission",
        "actor": {
            "principal_id": "controller_01",
            "owner_domain_id": "owner-domain_01",
            "granted_scopes": ["device.claim.revoke"],
            "authentication_strength": "software",
        },
        "owner_domain_id": "owner-domain_01",
        "business_owner_id": "owner_01",
        "scopes": ["device.claim.revoke"],
        "exp": 4_000_000_000,
    }

    whole = jwt.encode(required, secret, algorithm="HS256")
    context = await JwtAdmissionActorProvider(secret=secret)(_request(whole))
    assert context.actor.granted_scopes == ("device.claim.revoke",)

    for name in ("sub", "presenter", "actor", "owner_domain_id", "business_owner_id", "exp"):
        without = {key: value for key, value in required.items() if key != name}
        token = jwt.encode(without, secret, algorithm="HS256")
        with pytest.raises(AdmissionProblem) as refused:
            await JwtAdmissionActorProvider(secret=secret)(_request(token))
        assert refused.value.code == "UNAUTHENTICATED", name

    # The Management surface's vocabulary is not this surface's, whatever else
    # the token carries.
    management_shaped = jwt.encode(
        {
            "sub": "eidolon-admin/lifecycle-workflow",
            "presenter": "eidolon-admin/lifecycle-workflow",
            "aud": "eidolon-admission",
            "actor_ref": "controller:controller_01",
            "owner_id": "owner-domain_01",
            "roles": ["device-manager"],
            "scopes": ["device.claim.revoke"],
            "target_device_id": _DEVICE_INSTANCE_01,
            "exp": 4_000_000_000,
        },
        secret,
        algorithm="HS256",
    )
    with pytest.raises(AdmissionProblem):
        await JwtAdmissionActorProvider(secret=secret)(_request(management_shaped))
