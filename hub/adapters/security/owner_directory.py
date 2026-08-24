"""Filesystem adapter for the offline-issued Owner Domain directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eidolon_sdk.device_foundation.v1 import (
    AuthorityEndpoint,
    AuthorityLocator,
    LogicalAuthority,
    OwnerDomainDescriptor,
    OwnerDomainTrustAnchor,
)

from hub.config import OnboardingConfig


@dataclass(frozen=True, slots=True)
class OwnerDirectory:
    """Validated immutable directory plus its host-independent resolver."""

    descriptor: OwnerDomainDescriptor
    locator: AuthorityLocator

    @property
    def owner_domain_id(self) -> str:
        return self.descriptor.owner_domain_id

    def resolve(
        self,
        authority: LogicalAuthority,
        *,
        now: datetime,
    ) -> tuple[AuthorityEndpoint, ...]:
        return self.locator.resolve(self.owner_domain_id, authority, now=now)


def load_owner_directory(config: OnboardingConfig, *, now: datetime) -> OwnerDirectory:
    descriptor_path = Path(config.descriptor_path)
    root_path = Path(config.owner_root_certificate_path)
    authority_path = Path(config.authority_signing_certificate_path)
    for path, label in (
        (descriptor_path, "Owner Domain descriptor"),
        (root_path, "Owner root certificate"),
        (authority_path, "authority signing certificate"),
    ):
        if not path.is_file():
            raise RuntimeError(f"{label} is missing: {path}")
    descriptor = OwnerDomainDescriptor.model_validate_json(
        descriptor_path.read_text(encoding="utf-8")
    )
    if descriptor.owner_domain_id != config.owner_domain_id:
        raise RuntimeError("signed descriptor Owner Domain does not match Hub configuration")
    if descriptor.owner_domain_generation != config.owner_domain_generation:
        raise RuntimeError(
            "signed descriptor Owner Domain generation does not match Hub configuration"
        )
    anchor = OwnerDomainTrustAnchor(
        owner_domain_id=config.owner_domain_id,
        owner_root_certificate_pem=root_path.read_text(encoding="ascii"),
        authority_signing_certificate_pem=authority_path.read_text(encoding="ascii"),
        trust_epoch=config.trust_epoch,
    )
    locator = AuthorityLocator(anchor)
    locator.accept(descriptor, now=now)
    return OwnerDirectory(descriptor=descriptor, locator=locator)
