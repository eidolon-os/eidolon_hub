"""Protocol-neutral device aggregates and public directory values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

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
    last_enrollment_request_id: str = ""
    last_enrollment_fingerprint: str = ""
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

    def __post_init__(self) -> None:
        if not self.device_id.strip() or not self.owner_scope.strip():
            raise ValueError("directory device_id and owner_scope are required")
        if any(value.tzinfo is None for value in (self.enrolled_at, self.updated_at)):
            raise ValueError("directory timestamps must be timezone-aware")

    @property
    def manifest_revision(self) -> str:
        return self.manifest.revision
