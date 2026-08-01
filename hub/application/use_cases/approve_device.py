"""Approve and owner-scope a registered device."""

from __future__ import annotations

import json
from dataclasses import replace

from hub.domain.devices.entities import ManagedDevice
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import DeviceDirectoryProjector, DeviceRepository


class ApproveDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        events: EventBus,
        clock: Clock,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._events = events
        self._clock = clock
        self._directory_projector = directory_projector

    async def execute(self, *, device_id: str, owner_id: str, request_id: str) -> ManagedDevice:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        current = await self._devices.get(device_id)
        if current is None:
            raise KeyError(device_id)
        if current.revoked:
            raise ValueError("revoked device cannot be approved")
        fingerprint = f"approve:{owner_id}"
        if current.last_management_request_id == request_id:
            if current.last_management_fingerprint != fingerprint:
                raise ValueError("management request_id was reused")
            return current
        now = self._clock.now()
        approved = replace(
            current,
            owner_id=owner_id,
            approved=True,
            updated_at=now,
            last_management_request_id=request_id,
            last_management_fingerprint=fingerprint,
        )
        persisted = await self._devices.upsert(approved)
        await self._events.publish(
            DomainEvent(
                event_id=request_id,
                event_type="eidolon.device.approved.v1",
                source="eidolon-hub/device-management",
                subject=device_id,
                occurred_at=now,
                data_json=json.dumps({"owner_id": owner_id}, sort_keys=True),
            )
        )
        await self._directory_projector.execute(device_id)
        return persisted
