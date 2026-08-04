"""Revoke a device and notify its external Channel Provider."""

from __future__ import annotations

from dataclasses import replace

from hub.domain.channels.entities import ProviderChannelRevocation
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.channels import ChannelProviderControl
from hub.ports.identity import Clock
from hub.ports.management_events import (
    DeviceManagementEventRecord,
    DeviceManagementEventSink,
)
from hub.ports.repositories import DeviceDirectoryProjector, DeviceRepository


class RevokeDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        provider: ChannelProviderControl,
        hub_id: str,
        events: DeviceManagementEventSink,
        clock: Clock,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._provider = provider
        self._hub_id = hub_id
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
            if current.lifecycle_state is not DeviceLifecycleState.REVOKED:
                raise RuntimeError("revocation idempotency metadata is inconsistent")
            await self._revoke_provider(device_id=device_id, reason=reason, request_id=request_id)
            return current
        now = self._clock.now()
        revoked = replace(
            current,
            lifecycle_state=DeviceLifecycleState.REVOKED,
            updated_at=now,
            last_management_request_id=request_id,
            last_management_fingerprint=fingerprint,
        )
        persisted = await self._devices.upsert(revoked)
        await self._events.publish(
            DeviceManagementEventRecord(
                event_id=request_id,
                event_type="eidolon.device.revoked.v1",
                source="eidolon-hub/device-management",
                subject=device_id,
                occurred_at=now,
                data={"reason": reason},
            )
        )
        await self._directory_projector.execute(device_id)
        await self._revoke_provider(device_id=device_id, reason=reason, request_id=request_id)
        return persisted

    async def _revoke_provider(self, *, device_id: str, reason: str, request_id: str) -> None:
        await self._provider.revoke_channels(
            ProviderChannelRevocation(
                operation_id=request_id,
                hub_id=self._hub_id,
                device_id=device_id,
                reason=reason,
            )
        )
