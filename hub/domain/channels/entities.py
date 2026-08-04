"""Provider-neutral values relayed through the device handoff flow.

Hub validates assignments but owns neither channel policy nor channel state.
Device data belongs exclusively to the external Channel Provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ChannelKind(StrEnum):
    RELIABLE_DATA = "reliable-data"
    REALTIME_DATA = "realtime-data"
    AUDIO = "audio"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class OpaqueChannelBinding:
    """An encrypted/provider-owned blob Hub may relay but never inspect."""

    _value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("opaque channel binding is required")
        if len(self._value) > 64 * 1024:
            raise ValueError("opaque channel binding exceeds 64KiB")

    def relay_bytes(self) -> bytes:
        return self._value


@dataclass(frozen=True, slots=True)
class ChannelGrant:
    channel_id: str
    device_id: str
    purpose: str
    kinds: frozenset[ChannelKind]
    binding_format: str
    issued_at: datetime
    expires_at: datetime
    opaque_binding: OpaqueChannelBinding

    def __post_init__(self) -> None:
        if not self.channel_id.strip() or not self.device_id.strip() or not self.purpose.strip():
            raise ValueError("channel_id, device_id and purpose are required")
        if not self.kinds:
            raise ValueError("channel grant requires at least one kind")
        if not self.binding_format.strip() or len(self.binding_format) > 128:
            raise ValueError("a bounded binding_format is required")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("channel grant timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("channel grant expires_at must be after issued_at")

    def __repr__(self) -> str:
        return f"ChannelGrant(channel_id={self.channel_id!r}, opaque_binding=<redacted>)"


@dataclass(frozen=True, slots=True)
class ProviderDeviceContext:
    operation_id: str
    hub_id: str
    device_id: str
    owner_id: str
    display_name: str
    device_kind: str
    manifest_json: str = field(repr=False)
    manifest_revision: str

    def __post_init__(self) -> None:
        required = (
            self.operation_id,
            self.hub_id,
            self.device_id,
            self.device_kind,
            self.manifest_revision,
        )
        if any(not value.strip() for value in required):
            raise ValueError("provider device context identifiers are required")
        if not self.owner_id.strip():
            raise ValueError("provider owner_id is required")


@dataclass(frozen=True, slots=True)
class ChannelAssignmentSet:
    operation_id: str
    device_id: str
    manifest_revision: str
    grants: tuple[ChannelGrant, ...]

    def __post_init__(self) -> None:
        if not self.operation_id or not self.device_id or not self.manifest_revision:
            raise ValueError("channel assignment identifiers are required")
        channel_ids = [grant.channel_id for grant in self.grants]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("provider returned duplicate channel ids")


@dataclass(frozen=True, slots=True)
class ProviderChannelRevocation:
    operation_id: str
    hub_id: str
    device_id: str
    reason: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.operation_id, self.hub_id, self.device_id)):
            raise ValueError("channel revocation identifiers are required")
        if not self.reason.strip() or len(self.reason) > 256:
            raise ValueError("a bounded channel revocation reason is required")
