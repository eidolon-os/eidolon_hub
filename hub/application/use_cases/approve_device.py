"""Approve and owner-scope a registered device."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from hub.application.idempotency import mutation_fingerprint
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.identity import Clock
from hub.ports.management_events import (
    DeviceManagementEventRecord,
)
from hub.ports.repositories import (
    DeviceDirectoryProjector,
    DeviceMutationUnitOfWork,
    DeviceRepository,
)


class ApproveDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        mutations: DeviceMutationUnitOfWork,
        clock: Clock,
        handoff_ttl: timedelta,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._mutations = mutations
        self._clock = clock
        self._handoff_ttl = handoff_ttl
        self._directory_projector = directory_projector

    async def execute(
        self,
        *,
        device_id: str,
        owner_id: str,
        request_id: str,
        principal_id: str,
    ) -> ManagedDevice:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        current = await self._devices.get(device_id)
        if current is None:
            raise KeyError(device_id)
        if current.lifecycle_state is DeviceLifecycleState.REVOKED:
            raise ValueError("revoked device cannot be approved")
        fingerprint = mutation_fingerprint(
            "device.approve",
            {"owner_id": owner_id, "principal_id": principal_id},
        )
        if current.last_management_request_id == request_id:
            if current.last_management_fingerprint != fingerprint:
                raise ValueError("management request_id was reused")
            await self._directory_projector.execute(device_id)
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
            aggregate_revision=current.aggregate_revision + 1,
        )
        persisted = await self._mutations.commit(
            expected=current,
            device=approved,
            event=DeviceManagementEventRecord(
                event_id=request_id,
                event_type="eidolon.device.approved.v1",
                source="eidolon-hub/device-management",
                principal_id=principal_id,
                subject=device_id,
                occurred_at=now,
                data={"owner_id": owner_id},
            ),
        )
        await self._directory_projector.execute(device_id)
        return persisted
