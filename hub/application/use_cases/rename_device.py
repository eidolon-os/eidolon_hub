"""Give a device the name its Owner calls it by."""

from __future__ import annotations

from dataclasses import replace

from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.identity import Clock
from hub.ports.management_events import DeviceManagementEventRecord
from hub.ports.repositories import DeviceMutationUnitOfWork, DeviceRepository


class RenameDevice:
    """Set what a device is called, without touching what it is allowed to do.

    A device names itself when it enrols — an ESP32 reports its board, so two
    of them arrive indistinguishable — and until now nothing could ever change
    that. The name is the only part of a device record its Owner decides, and
    deciding it is not a lifecycle event: nothing is granted, withdrawn or
    re-scoped by calling something 客厅的 Box-3.

    That is why this does not touch the management idempotency slot. That slot
    exists so a repeated approval or revocation is recognised as the same act
    rather than performed twice, and writing a rename into it would make the
    next approval look like a replay of something else — which is exactly the
    failure a Controller hit as a permanent 409. Renaming is idempotent by
    nature: setting a name to what it already is changes nothing, and needs no
    ledger to say so.
    """

    def __init__(
        self,
        *,
        devices: DeviceRepository,
        mutations: DeviceMutationUnitOfWork,
        clock: Clock,
    ) -> None:
        self._devices = devices
        self._mutations = mutations
        self._clock = clock

    async def execute(
        self,
        *,
        device_id: str,
        owner_scope: str | None,
        display_name: str,
        principal_id: str,
    ) -> ManagedDevice:
        name = display_name.strip()
        if not name:
            raise ValueError("display_name cannot be blank")
        if len(name) > 128:
            raise ValueError("display_name must be at most 128 characters")
        current = await self._devices.get(device_id)
        if current is None:
            raise KeyError(device_id)
        # Same rule as revocation, for the same reason: the caller states whose
        # device this is and this record refuses to disagree.
        if owner_scope is not None and current.owner_id != owner_scope:
            raise PermissionError("device does not belong to that owner")
        if current.lifecycle_state is DeviceLifecycleState.REVOKED:
            raise ValueError("revoked device cannot be renamed")
        if current.display_name == name:
            return current
        now = self._clock.now()
        renamed = replace(
            current,
            display_name=name,
            updated_at=now,
            aggregate_revision=current.aggregate_revision + 1,
        )
        return await self._mutations.commit(
            expected=current,
            device=renamed,
            event=DeviceManagementEventRecord(
                event_id=f"rename:{device_id}:{now.isoformat()}",
                event_type="eidolon.device.renamed.v1",
                source="eidolon-hub/device-management",
                principal_id=principal_id,
                subject=device_id,
                occurred_at=now,
                data={"display_name": name},
            ),
        )
