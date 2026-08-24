from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
from types import SimpleNamespace

import pytest
import rfc8785
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from jose import jwt
from starlette.requests import Request

from hub.admission.auth import JwtAdmissionActorProvider
from hub.admission.domain import AdmissionProblem
from hub.composition.resources import load_commissioning_proof_verifier
from hub.config import CommissioningProofConfig, HubConfig


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


def test_manufacturer_profile_is_not_ready_without_real_verifier() -> None:
    verifier, ready = load_commissioning_proof_verifier(HubConfig())
    assert ready is False
    assert verifier.verify(
        device_instance_id="device_01",
        owner_domain_id="owner-domain_01",
        nonce="nonce_01",
        proof="opaque-proof",
        hardware_identity_evidence={"scheme": "manufacturer-p256", "evidence": "opaque"},
        operational_public_key="p256-spki:opaque",
    ) is None


def test_development_registry_is_explicit_root_owned_and_unknown_device_fails_closed(
    tmp_path, monkeypatch
) -> None:
    setup_secret = b"box-3-development-setup-secret"
    encoded = base64.urlsafe_b64encode(setup_secret).rstrip(b"=").decode()
    path = tmp_path / "commissioning-secrets.json"
    path.write_text(
        json.dumps(
            {
                "profile": "eidolon-development-hmac-commissioning-v1",
                "devices": {
                    "box-3": {
                        "setup_secret": encoded,
                        "hardware_identity_ref": "hardware-box-3",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    real_stat = type(path).stat

    def root_owned(candidate):
        value = real_stat(candidate)
        if candidate == path:
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o640)
        return value

    monkeypatch.setattr(type(path), "stat", root_owned)
    config = HubConfig(
        commissioning_proof=CommissioningProofConfig(
            profile="development-hmac",
            setup_secret_registry_path=str(path),
        )
    )
    verifier, ready = load_commissioning_proof_verifier(config)
    assert ready is True
    operational = ec.derive_private_key(17, ec.SECP256R1())
    operational_spki, operational_der = _spki(operational)
    instance_id = "device-instance-" + hashlib.sha256(operational_der).hexdigest()
    evidence_document = {
        "device_instance_id": instance_id,
        "hardware_lookup_id": "box-3",
        "operational_public_key": operational_spki,
        "profile_id": "eidolon-trust-p256-hpke-v1",
    }
    evidence = rfc8785.dumps(evidence_document).decode() + "." + _signature(
        operational, evidence_document
    )
    nonce = "physical-presence-nonce"
    proof = (
        base64.urlsafe_b64encode(
            hmac.new(
                setup_secret,
                f"box-3\0{instance_id}\0owner-local\0{nonce}".encode(),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    verified = verifier.verify(
        device_instance_id=instance_id,
        owner_domain_id="owner-local",
        nonce=nonce,
        proof=proof,
        hardware_identity_evidence={
            "scheme": "dev-self-signed-p256",
            "evidence": evidence,
        },
        operational_public_key=operational_spki,
    )
    assert verified is not None
    assert verified.hardware_identity_ref == "hardware-box-3"
    assert verifier.verify(
        device_instance_id=instance_id,
        owner_domain_id="owner-local",
        nonce=nonce,
        proof=proof,
        hardware_identity_evidence={
            "scheme": "dev-self-signed-p256",
            "evidence": evidence.replace("box-3", "unknown", 1),
        },
        operational_public_key=operational_spki,
    ) is None

    path.write_text(
        json.dumps(
            {
                "profile": "eidolon-development-hmac-commissioning-v1",
                "devices": {"box-3": encoded},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="registry is invalid"):
        load_commissioning_proof_verifier(config)
