"""Ports owned by Device Control, separate from Admission and Channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from hub.contracts.bindings.device import (
    DeviceLocalEraseAck,
    DeviceRef,
)

from .domain import DeviceEraseOperation


@dataclass(frozen=True, slots=True)
class DeviceClaimProjection:
    device_ref: DeviceRef
    state: str
    operational_public_key_spki: str


class DeviceClaimProjectionReader(Protocol):
    async def get_exact(self, *, device_ref: DeviceRef) -> DeviceClaimProjection | None: ...


class DeviceEraseLedger(Protocol):
    async def materialize_claim_events(self, *, now: datetime, operation_ttl: timedelta) -> int: ...

    async def expire_due(self, *, now: datetime) -> int: ...

    async def mark_accepted_pending(self, *, now: datetime) -> int: ...

    async def get(self, *, operation_id: str) -> DeviceEraseOperation | None: ...

    async def get_by_source_event(
        self, *, source_claim_event_id: str, device_ref: DeviceRef
    ) -> DeviceEraseOperation | None: ...

    async def get_for_device(self, *, device_ref: DeviceRef) -> DeviceEraseOperation | None: ...

    async def rearm_lapsed(
        self,
        *,
        operation_id: str,
        now: datetime,
        operation_ttl: timedelta,
    ) -> DeviceEraseOperation: ...

    async def accept_delivery(
        self,
        *,
        operation_id: str,
        delivery_attempt_id: str,
        accepted_at: datetime,
    ) -> DeviceEraseOperation: ...

    async def apply_ack(
        self,
        *,
        ack: DeviceLocalEraseAck,
        ack_fingerprint: str,
        received_at: datetime,
    ) -> DeviceEraseOperation: ...
