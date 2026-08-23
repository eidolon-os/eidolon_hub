"""Protocol-neutral device aggregates and public directory values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from hub.contracts.bindings.device import DeviceRef
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument


class DeviceLifecycleState(StrEnum):
    PENDING_APPROVAL = "pending-approval"
    APPROVED = "approved"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class ManagedDevice:
    """Authoritative onboarding, capability and policy aggregate."""

    identity: DeviceIdentity
    enrollment_id: str
    retrieval_token_hash: str = field(repr=False)
    retrieval_expires_at: datetime
    display_name: str
    device_kind: str
    manifest: DeviceManifestDocument
    enrolled_at: datetime
    updated_at: datetime
    owner_domain_generation: int = 1
    last_enrollment_request_id: str = ""
    last_enrollment_fingerprint: str = ""
    claim_generation: int = 1
    trust_epoch: int = 1
    aggregate_revision: int = 1
    owner_id: str | None = None
    lifecycle_state: DeviceLifecycleState = DeviceLifecycleState.PENDING_APPROVAL
    last_management_request_id: str = ""
    last_management_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.enrollment_id.strip() or not self.retrieval_token_hash.strip():
            raise ValueError("enrollment_id and retrieval token hash are required")
        if not self.device_kind.strip():
            raise ValueError("device_kind is required")
        if any(
            value.tzinfo is None
            for value in (self.retrieval_expires_at, self.enrolled_at, self.updated_at)
        ):
            raise ValueError("device timestamps must be timezone-aware")
        if self.owner_id is not None and not self.owner_id.strip():
            raise ValueError("owner_id must be null or non-empty")
        if min(
            self.owner_domain_generation,
            self.claim_generation,
            self.trust_epoch,
            self.aggregate_revision,
        ) < 1:
            raise ValueError("Owner, claim, trust and aggregate generations must be positive")
        if self.lifecycle_state is DeviceLifecycleState.APPROVED and self.owner_id is None:
            raise ValueError("approved device requires an owner")
        if (
            self.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
            and self.owner_id is not None
        ):
            raise ValueError("pending device cannot already have an owner")

    @property
    def manifest_json(self) -> str:
        return self.manifest.canonical_json

    @property
    def manifest_revision(self) -> str:
        return self.manifest.revision

    @property
    def device_ref(self) -> DeviceRef | None:
        if self.owner_id is None:
            return None
        return DeviceRef(
            device_instance_id=self.identity.device_id,
            owner_domain_id=self.owner_id,
            owner_domain_generation=self.owner_domain_generation,
            claim_generation=self.claim_generation,
            trust_epoch=self.trust_epoch,
            accepted_manifest_digest=self.manifest_revision,
        )


@dataclass(frozen=True, slots=True)
class DeviceEnrollmentIntent:
    request_id: str
    retrieval_token: str = field(repr=False)
    identity: DeviceIdentity
    display_name: str
    device_kind: str
    manifest: DeviceManifestDocument


@dataclass(frozen=True, slots=True)
class DeviceDirectoryEntry:
    """Safe, provider-neutral projection exposed to Eidolon OS consumers."""

    device_id: str
    owner_scope: str
    display_name: str
    device_kind: str
    manifest: DeviceManifestDocument
    lifecycle_state: DeviceLifecycleState
    enrolled_at: datetime
    updated_at: datetime
    #: When this device's current window closes. While it is pending approval
    #: that is the deadline for collecting the enrollment, and a listing has to
    #: know it: an enrollment past it can no longer be approved.
    retrieval_expires_at: datetime
    claim_generation: int
    trust_epoch: int
    owner_domain_generation: int = 1

    def __post_init__(self) -> None:
        if not self.device_id.strip() or not self.owner_scope.strip():
            raise ValueError("directory device_id and owner_scope are required")
        if any(
            value.tzinfo is None
            for value in (self.enrolled_at, self.updated_at, self.retrieval_expires_at)
        ):
            raise ValueError("directory timestamps must be timezone-aware")
        if min(
            self.owner_domain_generation, self.claim_generation, self.trust_epoch
        ) < 1:
            raise ValueError("directory Owner, claim and trust generations must be positive")

    @property
    def device_ref(self) -> DeviceRef:
        return DeviceRef(
            device_instance_id=self.device_id,
            owner_domain_id=self.owner_scope,
            owner_domain_generation=self.owner_domain_generation,
            claim_generation=self.claim_generation,
            trust_epoch=self.trust_epoch,
            accepted_manifest_digest=self.manifest_revision,
        )

    def awaits_approval(self, *, now: datetime) -> bool:
        """Whether approving this device could still do anything.

        A pending enrollment past its window is not waiting for anyone: the Hub
        refuses it, and the device replaces it by enrolling again. Offering it
        as claimable is offering something that can only fail.
        """

        return (
            self.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
            and now < self.retrieval_expires_at
        )

    @property
    def manifest_revision(self) -> str:
        return self.manifest.revision
