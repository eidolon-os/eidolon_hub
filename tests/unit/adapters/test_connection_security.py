from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hub.adapters.security.connection_proof import (
    HmacLeaseCredentialIssuer,
    P256ConnectionProofVerifier,
    canonical_connection_proof,
)
from hub.ports.identity import EnrollmentChallenge


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _challenge() -> EnrollmentChallenge:
    return EnrollmentChallenge(
        challenge_id="challenge-1",
        device_id="device-1",
        client_nonce="0123456789abcdef",
        server_nonce="fedcba9876543210",
        expires_at=datetime(2026, 8, 1, tzinfo=UTC),
        connector_id="mqtt-cloud",
        connector_kind="mqtt5",
        signaling_ref="mqtt:device-1",
        priority=100,
    )


@pytest.mark.asyncio
async def test_p256_challenge_proof_binds_device_and_both_nonces() -> None:
    challenge = _challenge()
    key = ec.generate_private_key(ec.SECP256R1())
    public_key = _b64(
        key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    )
    signature = _b64(key.sign(canonical_connection_proof(challenge), ec.ECDSA(hashes.SHA256())))

    fingerprint = await P256ConnectionProofVerifier().verify(
        challenge=challenge, public_key=public_key, signature=signature
    )

    assert fingerprint.startswith("p256:")
    with pytest.raises(PermissionError, match="invalid"):
        await P256ConnectionProofVerifier().verify(
            challenge=replace(challenge, device_id="device-2"),
            public_key=public_key,
            signature=signature,
        )


def test_lease_tokens_are_scoped_and_secrets_are_not_accepted_when_short() -> None:
    issuer = HmacLeaseCredentialIssuer(b"s" * 32)

    assert issuer.issue_lease_token(connection_id="a", device_id="device-1") != (
        issuer.issue_lease_token(connection_id="b", device_id="device-1")
    )
    with pytest.raises(ValueError, match="32 bytes"):
        HmacLeaseCredentialIssuer(b"short")
