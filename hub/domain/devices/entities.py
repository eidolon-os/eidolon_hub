"""Protocol-neutral device aggregates and public directory values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from hub.contracts.bindings.device import DeviceRef, OwnerDomainId
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument


class DeviceLifecycleState(StrEnum):
    APPROVED = "approved"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class ManagedDevice:
    """Owner-facing projection of one canonical Claim.

    Admission is authoritative for Claim lifecycle and identity.  This value
    only carries the query/rename projection required by device-management;
    it deliberately contains no enrollment or retrieval capability.
    """

    identity: DeviceIdentity
    display_name: str
    manifest_id: str
    manifest: DeviceManifestDocument
    enrolled_at: datetime
    updated_at: datetime
    owner_domain_id: str = "owner-test"
    owner_domain_generation: int = 1
    claim_generation: int = 1
    trust_epoch: int = 1
    aggregate_revision: int = 1
    owner_id: str | None = None
    lifecycle_state: DeviceLifecycleState = DeviceLifecycleState.APPROVED
    last_management_request_id: str = ""
    last_management_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.manifest_id.strip():
            raise ValueError("manifest_id is required")
        if any(value.tzinfo is None for value in (self.enrolled_at, self.updated_at)):
            raise ValueError("device timestamps must be timezone-aware")
        if self.owner_id is not None and not self.owner_id.strip():
            raise ValueError("owner_id must be null or non-empty")
        if (
            min(
                self.owner_domain_generation,
                self.claim_generation,
                self.trust_epoch,
                self.aggregate_revision,
            )
            < 1
        ):
            raise ValueError("Owner, claim, trust and aggregate generations must be positive")
        if self.owner_id is None:
            raise ValueError("Claim projection requires an owner")

    @property
    def manifest_json(self) -> str:
        return self.manifest.canonical_json

    @property
    def manifest_digest(self) -> str:
        return self.manifest.digest

    @property
    def manifest_declared_revision(self) -> int:
        return self.manifest.declared_revision

    @property
    def device_ref(self) -> DeviceRef | None:
        if self.owner_id is None:
            return None
        return DeviceRef(
            device_instance_id=self.identity.device_id,
            owner_domain_id=OwnerDomainId(self.owner_domain_id),
            owner_domain_generation=self.owner_domain_generation,
            claim_generation=self.claim_generation,
            trust_epoch=self.trust_epoch,
        )


@dataclass(frozen=True, slots=True)
class DeviceDirectoryEntry:
    """Safe, provider-neutral projection exposed to Eidolon OS consumers."""

    device_id: str
    owner_scope: str
    display_name: str
    manifest_id: str
    manifest: DeviceManifestDocument
    lifecycle_state: DeviceLifecycleState
    enrolled_at: datetime
    updated_at: datetime
    claim_generation: int
    trust_epoch: int
    owner_domain_generation: int = 1
    owner_domain_id: str = "owner-test"

    def __post_init__(self) -> None:
        if not self.device_id.strip() or not self.owner_scope.strip():
            raise ValueError("directory device_id and owner_scope are required")
        if any(value.tzinfo is None for value in (self.enrolled_at, self.updated_at)):
            raise ValueError("directory timestamps must be timezone-aware")
        if min(self.owner_domain_generation, self.claim_generation, self.trust_epoch) < 1:
            raise ValueError("directory Owner, claim and trust generations must be positive")

    @property
    def device_ref(self) -> DeviceRef:
        return DeviceRef(
            device_instance_id=self.device_id,
            owner_domain_id=OwnerDomainId(self.owner_domain_id),
            owner_domain_generation=self.owner_domain_generation,
            claim_generation=self.claim_generation,
            trust_epoch=self.trust_epoch,
        )

    @property
    def manifest_digest(self) -> str:
        return self.manifest.digest
