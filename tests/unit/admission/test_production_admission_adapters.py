from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
from types import SimpleNamespace

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
from hub.admission.domain import AdmissionProblem
from hub.composition.resources import load_commissioning_proof_verifier
from hub.config import CommissioningProofConfig, HubConfig

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_INSTANCE_01 = named_device_instance_id("device-instance-01")
_DEVICE_01 = named_device_instance_id("device_01")


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
        device_instance_id=_DEVICE_01,
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
                "profile": "eidolon-development-hmac-commissioning-v2",
                "devices": {"box-3": {"setup_secret": encoded}},
            }
        ),
        encoding="utf-8",
    )
    real_stat = type(path).stat

    # Patching Path.stat replaces it for every path, pytest's own traceback
    # rendering included, so the stub has to keep the real signature; without
    # it a genuine failure in this test crashed the reporter instead of being
    # reported.
    def root_owned(candidate, **kwargs):
        value = real_stat(candidate, **kwargs)
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
    instance_id = derive_device_instance_id(operational_spki)
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
    assert verified.hardware_lookup_id == "box-3"
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
                "profile": "eidolon-development-hmac-commissioning-v2",
                "devices": {"box-3": encoded},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="registry is invalid"):
        load_commissioning_proof_verifier(config)

    # A v1 file asserted a hand-written hardware_identity_ref per device, which
    # is how a Waveshare AMOLED board ended up permanently claiming to be an
    # ESP-BOX-3. Such a file must fail closed rather than be read with the
    # unverifiable field quietly ignored.
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
    with pytest.raises(RuntimeError, match="registry is invalid"):
        load_commissioning_proof_verifier(config)

    path.write_text(
        json.dumps(
            {
                "profile": "eidolon-development-hmac-commissioning-v2",
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
    with pytest.raises(RuntimeError, match="registry is invalid"):
        load_commissioning_proof_verifier(config)

    # The profile names the format, so entries alone are not enough: a file
    # still labelled v1 has not been reviewed against the rule that an entry
    # may not assert a hardware identity, and is not read on its say-so.
    path.write_text(
        json.dumps(
            {
                "profile": "eidolon-development-hmac-commissioning-v1",
                "devices": {"box-3": {"setup_secret": encoded}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="registry is invalid"):
        load_commissioning_proof_verifier(config)


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
