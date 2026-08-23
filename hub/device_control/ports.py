"""Ports owned by Device Control, separate from Admission and Channel."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from hub.contracts.bindings.device import (
    DeviceLocalEraseAck,
    DeviceOperationKeyProof,
    DeviceRef,
)

from .domain import DeviceEraseOperation


class DeviceEraseLedger(Protocol):
    async def bind_operation_key(
        self,
        *,
        enrollment_id: str,
        claim_generation: int,
        proof: DeviceOperationKeyProof,
        key_id: str,
        bound_at: datetime,
    ) -> None: ...

    async def materialize_claim_events(
        self, *, now: datetime, operation_ttl: timedelta
    ) -> int: ...

    async def expire_due(self, *, now: datetime) -> int: ...

    async def mark_accepted_pending(self, *, now: datetime) -> int: ...

    async def get(self, *, operation_id: str) -> DeviceEraseOperation | None: ...

    async def get_for_device(
        self, *, device_ref: DeviceRef
    ) -> DeviceEraseOperation | None: ...

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
