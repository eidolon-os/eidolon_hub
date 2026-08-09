from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hub.adapters.security.enrollment_identity import (
    P256EnrollmentIdentityVerifier,
    enrollment_proof_statement,
)
from hub.contracts.bindings.onboarding import DeviceEnrollment


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _signed_enrollment(*, pairing_commitment: str = "sha256:" + "a" * 64):
    key = ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    unsigned = DeviceEnrollment(
        request_id="enroll-1",
        retrieval_token="device-generated-random-token-000001",
        identity={"device_id": "device-1"},
        manifest={"schema_version": 1, "title": "Device"},
        display_name="Device",
        device_kind="generic",
        pairing_proof={
            "method": "local-secret-sha256",
            "commitment": pairing_commitment,
        },
    )
    signature = key.sign(enrollment_proof_statement(unsigned), ec.ECDSA(hashes.SHA256()))
    payload = unsigned.model_dump()
    payload["identity_proof"] = {
        "algorithm": "p256-sha256",
        "public_key_spki": _base64url(public_der),
        "signature": _base64url(signature),
    }
    return DeviceEnrollment.model_validate(payload)


def test_p256_enrollment_proof_binds_pairing_and_device_statement() -> None:
    enrollment = DeviceEnrollment.model_validate(_signed_enrollment().model_dump())
    fingerprint = P256EnrollmentIdentityVerifier().verify(enrollment)

    assert fingerprint.startswith("p256:")
    assert len(fingerprint) == 69

    tampered_payload = enrollment.model_dump()
    tampered_payload["pairing_proof"] = {
        "method": "local-secret-sha256",
        "commitment": "sha256:" + "b" * 64,
    }
    tampered = DeviceEnrollment.model_validate(tampered_payload)
    with pytest.raises(PermissionError, match="identity proof"):
        P256EnrollmentIdentityVerifier().verify(tampered)


def test_enrollment_statement_matches_esp32_wire_vector() -> None:
    enrollment = DeviceEnrollment(
        request_id="enroll-a",
        retrieval_token="device-generated-random-token-000001",
        identity={"device_id": "aa:bb"},
        manifest={
            "schema_version": 1,
            "title": "esp32-s3-touch-amoled-2.06",
            "properties": [],
            "actions": [],
            "events": [],
            "media": [
                {
                    "kind": "audio",
                    "direction": "bidirectional",
                    "codecs": ["opus"],
                }
            ],
        },
        display_name="esp32-s3-touch-amoled-2.06",
        device_kind="esp32-s3-touch-amoled-2.06",
        pairing_proof={
            "method": "local-secret-sha256",
            "commitment": "sha256:" + "a" * 64,
        },
    )

    assert enrollment_proof_statement(enrollment) == (
        b"eidolon-device-enrollment-proof-v1\n"
        b"8:enroll-a\n"
        b"5:aa:bb\n"
        b"71:sha256:8a3f8ba8e8f044009aef31324b1a6073e5c20992b8a6b468cd9153bb40a4152d\n"
        b"19:local-secret-sha256\n"
        b"71:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        b"26:esp32-s3-touch-amoled-2.06\n"
        b"26:esp32-s3-touch-amoled-2.06\n"
        b"71:sha256:61fbd6624779ccded820c200cb9f289dde23860ec6952a52e5068824758d0175\n"
    )


def test_pairing_commitment_without_device_key_proof_is_rejected() -> None:
    enrollment = DeviceEnrollment(
        request_id="enroll-1",
        retrieval_token="device-generated-random-token-000001",
        identity={"device_id": "device-1"},
        manifest={"schema_version": 1, "title": "Device"},
        pairing_proof={
            "method": "local-secret-sha256",
            "commitment": "sha256:" + "a" * 64,
        },
    )

    with pytest.raises(PermissionError, match="requires device identity proof"):
        P256EnrollmentIdentityVerifier().verify(enrollment)
