"""Approve and owner-scope a registered device."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.identity import Clock
from hub.ports.management_events import (
    DeviceManagementEventRecord,
    DeviceManagementEventSink,
)
from hub.ports.repositories import DeviceDirectoryProjector, DeviceRepository


class ApproveDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        events: DeviceManagementEventSink,
        clock: Clock,
        handoff_ttl: timedelta,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._events = events
        self._clock = clock
        self._handoff_ttl = handoff_ttl
        self._directory_projector = directory_projector

    async def execute(self, *, device_id: str, owner_id: str, request_id: str) -> ManagedDevice:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        current = await self._devices.get(device_id)
        if current is None:
            raise KeyError(device_id)
        if current.lifecycle_state is DeviceLifecycleState.REVOKED:
            raise ValueError("revoked device cannot be approved")
        fingerprint = f"approve:{owner_id}"
        if current.last_management_request_id == request_id:
            if current.last_management_fingerprint != fingerprint:
                raise ValueError("management request_id was reused")
            return current
        now = self._clock.now()
        if (
            current.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
            and current.retrieval_expires_at <= now
        ):
            raise ValueError("device enrollment expired")
        if (
            current.lifecycle_state is DeviceLifecycleState.APPROVED
            and current.owner_id != owner_id
        ):
            raise ValueError("approved device owner cannot be replaced")
        approved = replace(
            current,
            owner_id=owner_id,
            lifecycle_state=DeviceLifecycleState.APPROVED,
            retrieval_expires_at=now + self._handoff_ttl,
            updated_at=now,
            last_management_request_id=request_id,
            last_management_fingerprint=fingerprint,
        )
        persisted = await self._devices.upsert(approved)
        await self._events.publish(
            DeviceManagementEventRecord(
                event_id=request_id,
                event_type="eidolon.device.approved.v1",
                source="eidolon-hub/device-management",
                subject=device_id,
                occurred_at=now,
                data={"owner_id": owner_id},
            )
        )
        await self._directory_projector.execute(device_id)
        return persisted
