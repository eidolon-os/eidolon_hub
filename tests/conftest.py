"""Pytest configuration and shared fixtures for eidolon_hub tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from eidolon_sdk.device_foundation.v1 import (
    AuthorityEndpoint,
    LogicalAuthority,
    OwnerDomainDescriptor,
    descriptor_key_id,
    sign_descriptor,
)

from hub.config import OnboardingConfig


@pytest.fixture
def owner_directory_config(tmp_path):
    """Issue test-only public runtime material from an offline signing scope."""

    def build(
        *,
        owner_domain_id: str = "owner-test",
        host: str = "hub.test",
    ) -> OnboardingConfig:
        root_key = ec.derive_private_key(0x123456789ABCDEF, ec.SECP256R1())
        signer_key = ec.derive_private_key(0x3456789ABCDEF12, ec.SECP256R1())
        owner_name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Test Owner Root")]
        )
        root = (
            x509.CertificateBuilder()
            .subject_name(owner_name)
            .issuer_name(owner_name)
            .public_key(root_key.public_key())
            .serial_number(100)
            .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2040, 1, 1, tzinfo=UTC))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .sign(root_key, hashes.SHA256())
        )
        authority = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, "Test Directory Signer")]
                )
            )
            .issuer_name(owner_name)
            .public_key(signer_key.public_key())
            .serial_number(101)
            .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2040, 1, 1, tzinfo=UTC))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())
        )
        root_spki = root_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        signer_spki = signer_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        descriptor = sign_descriptor(
            OwnerDomainDescriptor(
                owner_domain_id=owner_domain_id,
                directory_revision=1,
                trust_root_refs=(descriptor_key_id(root_spki),),
                endpoints=(
                    AuthorityEndpoint(
                        authority=LogicalAuthority.ADMISSION,
                        logical_audience="eidolon-admission",
                        uri=f"https://{host}/api/device-onboarding/v1",
                        transport_profile="https-json",
                        priority=10,
                    ),
                    AuthorityEndpoint(
                        authority=LogicalAuthority.DEVICE_CONTROL,
                        logical_audience="eidolon-device-control",
                        uri=f"https://{host}/api/device-control/v1",
                        transport_profile="https-json",
                        priority=10,
                    ),
                ),
                issued_at=datetime(2020, 1, 1, tzinfo=UTC),
                expires_at=datetime(2040, 1, 1, tzinfo=UTC),
                signing_key_id=descriptor_key_id(signer_spki),
                signature="A" * 86,
            ),
            signer_key,
        )
        material = tmp_path / f"owner-directory-{owner_domain_id}"
        material.mkdir(exist_ok=True)
        descriptor_path = material / "descriptor.json"
        root_path = material / "owner-root.pem"
        authority_path = material / "authority.pem"
        descriptor_path.write_text(
            json.dumps(descriptor.model_dump(mode="json")), encoding="utf-8"
        )
        root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
        authority_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
        return OnboardingConfig(
            owner_domain_id=owner_domain_id,
            descriptor_uri=f"https://{host}/api/device-onboarding/v1/descriptor",
            descriptor_path=str(descriptor_path),
            owner_root_certificate_path=str(root_path),
            authority_signing_certificate_path=str(authority_path),
        )

    return build
