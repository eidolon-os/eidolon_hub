"""Ports and immutable values for the Admission Claim lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from hub.domain.devices.entities import DeviceRef, ManagedDevice


@dataclass(frozen=True, slots=True)
class ClaimCommandResult:
    command_id: str
    fingerprint: str
    outcome: str
    device_ref: DeviceRef
    aggregate_revision: int
    occurred_at: datetime
    event_id: str | None

    def __post_init__(self) -> None:
        if self.outcome not in {"committed", "replayed"}:
            raise ValueError("unsupported Claim command outcome")
        if not self.command_id.strip() or not self.fingerprint.startswith("sha256:"):
            raise ValueError("Claim command identity and fingerprint are required")
        if len(self.fingerprint) != 71 or any(
            value not in "0123456789abcdef" for value in self.fingerprint[7:]
        ):
            raise ValueError("Claim command fingerprint must be SHA-256")
        if self.aggregate_revision < 1:
            raise ValueError("Claim aggregate revision must be positive")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Claim command result timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ClaimEventRecord:
    event_id: str
    event_type: str
    device_ref: DeviceRef
    aggregate_revision: int
    correlation_id: str
    causation_id: str
    actor_principal_id: str
    occurred_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if self.event_type != "live.eidolon.device.claim-revoked.v1":
            raise ValueError("unsupported Claim event type")
        if self.aggregate_revision < 1:
            raise ValueError("Claim event aggregate revision must be positive")
        if any(
            not value.strip()
            for value in (
                self.event_id,
                self.correlation_id,
                self.causation_id,
                self.actor_principal_id,
                self.reason,
            )
        ):
            raise ValueError("Claim event identity and reason are required")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Claim event timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StoredClaimEvent:
    stream_position: int
    event: ClaimEventRecord

    def __post_init__(self) -> None:
        if self.stream_position < 1:
            raise ValueError("Claim event stream position must be positive")


class ClaimLifecycleStore(Protocol):
    async def get_command(
        self, *, owner_domain_id: str, command_type: str, command_id: str
    ) -> ClaimCommandResult | None: ...

    async def commit_revoke(
        self,
        *,
        expected: ManagedDevice,
        revoked: ManagedDevice,
        command_id: str,
        fingerprint: str,
        event: ClaimEventRecord,
    ) -> ClaimCommandResult: ...

    async def commit_terminal_result(
        self,
        *,
        device: ManagedDevice,
        command_id: str,
        fingerprint: str,
        occurred_at: datetime,
    ) -> ClaimCommandResult: ...

    async def list_events_after(
        self, *, stream_position: int, limit: int
    ) -> tuple[StoredClaimEvent, ...]: ...
