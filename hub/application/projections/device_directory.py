"""Project durable device facts into the in-memory public directory."""

from __future__ import annotations

from hub.domain.devices.entities import DeviceDirectoryEntry
from hub.ports.repositories import DeviceDirectoryRepository, DeviceRepository


class ProjectDeviceDirectory:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        directory: DeviceDirectoryRepository,
    ) -> None:
        self._devices = devices
        self._directory = directory

    async def execute(self, device_id: str) -> DeviceDirectoryEntry:
        device = await self._devices.get(device_id)
        if device is None:
            raise KeyError(device_id)
        entry = DeviceDirectoryEntry(
            device_id=device_id,
            owner_scope=device.owner_id or "unclaimed",
            display_name=device.display_name,
            device_kind=device.device_kind,
            manifest=device.manifest,
            lifecycle_state=device.lifecycle_state,
            enrolled_at=device.enrolled_at,
            updated_at=device.updated_at,
        )
        return await self._directory.upsert(entry)

    async def execute_all(self) -> tuple[DeviceDirectoryEntry, ...]:
        """Hydrate the hot directory from authoritative device facts."""

        projected: list[DeviceDirectoryEntry] = []
        for device in await self._devices.list_all():
            projected.append(await self.execute(device.identity.device_id))
        return tuple(projected)
