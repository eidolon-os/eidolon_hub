"""Provider-neutral communication-channel domain values."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from hub.domain.commands.entities import CommandState


class ChannelKind(StrEnum):
    RELIABLE_DATA = "reliable-data"
    REALTIME_DATA = "realtime-data"
    AUDIO = "audio"
    VIDEO = "video"


class ChannelState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"
    FAILED = "failed"


class ProviderSyncState(StrEnum):
    PENDING = "pending"
    SYNCHRONIZING = "synchronizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
    purpose: str
    kinds: frozenset[ChannelKind]
    binding_format: str
    issued_at: datetime
    expires_at: datetime
    state: ChannelState = ChannelState.PENDING
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.channel_id.strip() or not self.device_id.strip() or not self.purpose.strip():
            raise ValueError("channel_id, device_id and purpose are required")
        if not self.kinds:
            raise ValueError("channel lease requires at least one kind")
        if not self.binding_format.strip() or len(self.binding_format) > 128:
            raise ValueError("a bounded binding_format is required")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("channel lease timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("channel lease expires_at must be after issued_at")
        if self.updated_at is not None and self.updated_at.tzinfo is None:
            raise ValueError("channel lease updated_at must be timezone-aware")

    def is_active(self, now: datetime) -> bool:
        return self.state is ChannelState.ACTIVE and self.expires_at > now

    def transition(self, state: ChannelState, *, occurred_at: datetime) -> "ChannelLease":
        if occurred_at.tzinfo is None:
            raise ValueError("channel lifecycle timestamp must be timezone-aware")
        if occurred_at < self.issued_at:
            raise ValueError("channel lifecycle occurred before channel issuance")
        if state is ChannelState.ACTIVE and occurred_at >= self.expires_at:
            raise ValueError("expired channel cannot become active")
        allowed = {
            ChannelState.PENDING: {
                ChannelState.PENDING,
                ChannelState.ACTIVE,
                ChannelState.CLOSED,
                ChannelState.FAILED,
            },
            ChannelState.ACTIVE: {ChannelState.ACTIVE, ChannelState.CLOSED, ChannelState.FAILED},
            ChannelState.CLOSED: {ChannelState.CLOSED},
            ChannelState.FAILED: {ChannelState.FAILED},
        }
        if state not in allowed[self.state]:
            raise ValueError(f"invalid channel transition: {self.state.value} -> {state.value}")
        if self.updated_at is not None and occurred_at < self.updated_at:
            raise ValueError("stale channel lifecycle event")
        return replace(self, state=state, updated_at=occurred_at)


@dataclass(frozen=True, slots=True)
class ChannelGrant:
    operation_id: str
    lease: ChannelLease
    opaque_binding: OpaqueChannelBinding

    def __repr__(self) -> str:
        return (
            f"ChannelGrant(operation_id={self.operation_id!r}, "
            f"channel_id={self.lease.channel_id!r}, opaque_binding=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ProviderDeviceContext:
    operation_id: str
    hub_id: str
    device_id: str
    public_key_fingerprint: str
    tenant_id: str
    owner_id: str | None
    display_name: str
    device_kind: str
    manifest_json: str = field(repr=False)
    manifest_revision: str
    approved: bool
    revoked: bool
    connected: bool

    def __post_init__(self) -> None:
        required = (
            self.operation_id,
            self.hub_id,
            self.device_id,
            self.public_key_fingerprint,
            self.tenant_id,
            self.device_kind,
            self.manifest_revision,
        )
        if any(not value.strip() for value in required):
            raise ValueError("provider device context identifiers are required")
        if self.owner_id is not None and not self.owner_id.strip():
            raise ValueError("provider owner_id must be null or non-empty")


@dataclass(frozen=True, slots=True)
class ChannelAssignmentSet:
    operation_id: str
    device_id: str
    manifest_revision: str
    grants: tuple[ChannelGrant, ...]

    def __post_init__(self) -> None:
        if not self.operation_id or not self.device_id or not self.manifest_revision:
            raise ValueError("channel assignment identifiers are required")
        channel_ids = [grant.lease.channel_id for grant in self.grants]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("provider returned duplicate channel ids")


@dataclass(frozen=True, slots=True)
class ProviderSyncRecord:
    device_id: str
    operation_id: str
    desired_revision: str
    state: ProviderSyncState
    attempts: int
    updated_at: datetime
    owner_instance_id: str = ""
    claim_expires_at: datetime | None = None
    last_error: str = ""

    def __post_init__(self) -> None:
        if not self.device_id or not self.operation_id or not self.desired_revision:
            raise ValueError("provider sync identifiers are required")
        if self.attempts < 0 or self.updated_at.tzinfo is None:
            raise ValueError("invalid provider sync attempt metadata")
        if self.claim_expires_at is not None and self.claim_expires_at.tzinfo is None:
            raise ValueError("provider sync claim expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ChannelLifecycle:
    channel_id: str
    device_id: str
    state: ChannelState
    occurred_at: datetime
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.channel_id or not self.device_id:
            raise ValueError("channel lifecycle identifiers are required")
        if self.state is ChannelState.PENDING:
            raise ValueError("Provider cannot report a pending lifecycle event")
        if self.occurred_at.tzinfo is None:
            raise ValueError("channel lifecycle timestamp must be timezone-aware")
        if len(self.reason) > 256:
            raise ValueError("channel lifecycle reason is too long")


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
