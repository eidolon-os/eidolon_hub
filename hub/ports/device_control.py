"""Durable Channel/Delivery control work derived from Claim events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from hub.domain.devices.entities import DeviceRef


@dataclass(frozen=True, slots=True)
class DeviceControlOperation:
    event_id: str
    operation_type: str
    operation_id: str
    device_ref: DeviceRef
    manifest_digest: str
    reason: str
    state: str
    attempt_count: int
    next_attempt_at: datetime
    delivered_at: datetime | None = None
    last_error: str = ""

    def __post_init__(self) -> None:
        if self.operation_type != "channel.device-access.revoke":
            raise ValueError("unsupported Device Control operation type")
        if self.state not in {"pending", "delivered"}:
            raise ValueError("unsupported Device Control operation state")
        if self.attempt_count < 0:
            raise ValueError("Device Control attempt count must not be negative")
        if self.next_attempt_at.tzinfo is None or self.next_attempt_at.utcoffset() is None:
            raise ValueError("Device Control retry timestamp must be timezone-aware")
        if self.state == "delivered" and self.delivered_at is None:
            raise ValueError("delivered Device Control operation requires evidence time")


class DeviceControlStore(Protocol):
    async def materialize_claim_events(self, *, now: datetime) -> int: ...

    async def list_due(
        self, *, now: datetime, limit: int
    ) -> tuple[DeviceControlOperation, ...]: ...

    async def get_by_event_id(
        self, *, event_id: str
    ) -> DeviceControlOperation | None: ...

    async def mark_delivered(
        self, *, event_id: str, delivered_at: datetime
    ) -> None: ...

    async def mark_retry(
        self,
        *,
        event_id: str,
        attempt_count: int,
        next_attempt_at: datetime,
        error: str,
    ) -> None: ...
