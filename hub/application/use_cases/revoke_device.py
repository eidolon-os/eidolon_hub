"""Revoke a device and its Hub-owned connections."""

from __future__ import annotations

import json
from dataclasses import replace

from hub.domain.devices.entities import ManagedDevice
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import (
    ConnectionRepository,
    DeviceDirectoryProjector,
    DeviceRepository,
)


class RevokeDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        connections: ConnectionRepository,
        events: EventBus,
        clock: Clock,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._connections = connections
        self._events = events
        self._clock = clock
        self._directory_projector = directory_projector

    async def execute(self, *, device_id: str, reason: str, request_id: str) -> ManagedDevice:
        current = await self._devices.get(device_id)
        if current is None:
            raise KeyError(device_id)
        fingerprint = f"revoke:{reason}"
        if current.last_management_request_id == request_id:
            if current.last_management_fingerprint != fingerprint:
                raise ValueError("management request_id was reused")
            return current
        now = self._clock.now()
        revoked = replace(
            current,
            approved=False,
            revoked=True,
            updated_at=now,
            last_management_request_id=request_id,
            last_management_fingerprint=fingerprint,
        )
        persisted = await self._devices.upsert(revoked)
        for lease in await self._connections.active_for_device(device_id, now=now):
            await self._connections.upsert(lease.close())
        await self._events.publish(
            DomainEvent(
                event_id=request_id,
                event_type="eidolon.device.revoked.v1",
                source="eidolon-hub/device-management",
                subject=device_id,
                occurred_at=now,
                data_json=json.dumps({"reason": reason}, sort_keys=True),
            )
        )
        await self._directory_projector.execute(device_id)
        return persisted
