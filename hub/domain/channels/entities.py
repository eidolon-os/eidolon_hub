"""Provider-neutral communication-channel domain values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from hub.domain.commands.entities import CommandState


class ChannelKind(StrEnum):
    RELIABLE_DATA = "reliable-data"
    REALTIME_DATA = "realtime-data"
    AUDIO = "audio"
    VIDEO = "video"


class ChannelNegotiationOperation(StrEnum):
    OFFER = "channel.offer"
    ACCEPT = "channel.accept"
    CLOSE = "channel.close"


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    name: str
    required_kinds: frozenset[ChannelKind]
    provisioner_ref: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("channel profile name is required")
        if not self.required_kinds:
            raise ValueError("channel profile requires at least one kind")
        if not self.provisioner_ref.strip():
            raise ValueError("provisioner_ref is required")


@dataclass(frozen=True, slots=True)
class ChannelRequest:
    request_id: str
    device_id: str
    profile: ChannelProfile
    requested_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.device_id.strip():
            raise ValueError("request_id and device_id are required")
        if self.requested_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("channel request timestamps must be timezone-aware")
        if self.expires_at <= self.requested_at:
            raise ValueError("channel request must expire after it is requested")


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
class ChannelLease:
    channel_id: str
    device_id: str
    profile_name: str
    issued_at: datetime
    expires_at: datetime
    renew_after: datetime | None = None

    def __post_init__(self) -> None:
        if not self.channel_id.strip() or not self.device_id.strip():
            raise ValueError("channel_id and device_id are required")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("channel lease timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("channel lease expires_at must be after issued_at")


@dataclass(frozen=True, slots=True)
class ChannelGrant:
    request_id: str
    lease: ChannelLease
    opaque_binding: OpaqueChannelBinding

    def __repr__(self) -> str:
        return (
            f"ChannelGrant(request_id={self.request_id!r}, "
            f"channel_id={self.lease.channel_id!r}, opaque_binding=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class InboundCommandData:
    """Marker retained so the core, not a transport, rejects device commands."""


@dataclass(frozen=True, slots=True)
class CommandAckData:
    command_id: str
    target_state: CommandState
    error: str = ""


@dataclass(frozen=True, slots=True)
class CommandResultData:
    command_id: str
    target_state: CommandState
    result_json: str | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class ReportedStateData:
    revision: int
    values_json: str


@dataclass(frozen=True, slots=True)
class DeviceEventData:
    event_id: str
    name: str
    payload_json: str


ChannelDataPayload = (
    InboundCommandData | CommandAckData | CommandResultData | ReportedStateData | DeviceEventData
)


@dataclass(frozen=True, slots=True)
class ChannelDataEnvelope:
    envelope_id: str
    channel_id: str
    device_id: str
    sequence: int
    occurred_at: datetime
    payload: ChannelDataPayload

    def __post_init__(self) -> None:
        if not self.envelope_id or not self.channel_id or not self.device_id:
            raise ValueError("channel envelope identifiers are required")
        if self.sequence < 1:
            raise ValueError("channel envelope sequence must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("channel envelope occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ChannelNegotiationIntent:
    operation: ChannelNegotiationOperation
    request_id: str
    device_id: str
    connection_id: str
    channel_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.request_id or not self.device_id or not self.connection_id:
            raise ValueError("channel signal identifiers are required")
        if self.operation is not ChannelNegotiationOperation.OFFER and not self.channel_id:
            raise ValueError("channel_id is required for accept and close")
