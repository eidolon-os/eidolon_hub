"""Device inventory entities and persistence transfer records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument


@dataclass(frozen=True, slots=True)
class ManagedDevice:
    """Protocol-neutral aggregate persisted through ``DeviceRepository``."""

    identity: DeviceIdentity
    display_name: str
    device_kind: str
    manifest: DeviceManifestDocument
    registered_at: datetime
    updated_at: datetime
    last_registration_request_id: str = ""
    owner_id: str | None = None
    approved: bool = False
    revoked: bool = False
    last_management_request_id: str = ""
    last_management_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.device_kind.strip():
            raise ValueError("device_kind is required")
        if self.registered_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("device timestamps must be timezone-aware")

    @property
    def manifest_json(self) -> str:
        return self.manifest.canonical_json

    @property
    def manifest_revision(self) -> str:
        return self.manifest.revision


@dataclass(frozen=True, slots=True)
class DeviceRegistrationIntent:
    request_id: str
    identity: DeviceIdentity
    display_name: str
    device_kind: str
    manifest: DeviceManifestDocument

    @property
    def manifest_json(self) -> str:
        return self.manifest.canonical_json

    @property
    def manifest_revision(self) -> str:
        return self.manifest.revision


@dataclass(frozen=True, slots=True)
class DirectorySession:
    session_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("directory session_id is required")
        if self.expires_at.tzinfo is None:
            raise ValueError("directory session expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DeviceDirectoryEntry:
    """Provider-neutral public-blackboard value."""

    device_id: str
    owner_scope: str
    display_name: str
    device_kind: str
    manifest_json: str
    manifest_revision: str
    approved: bool
    revoked: bool
    online: bool
    sessions: tuple[DirectorySession, ...]
    registered_at: datetime
    updated_at: datetime
    revision: int = 1
