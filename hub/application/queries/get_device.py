"""Get one device directory entry by stable identity and owner scope."""

from __future__ import annotations

from hub.domain.devices.entities import DeviceDirectoryEntry
from hub.ports.repositories import DeviceDirectoryRepository


class GetDevice:
    def __init__(self, directory: DeviceDirectoryRepository) -> None:
        self._directory = directory

    async def execute(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry:
        if not owner_scope.strip() or not device_id.strip():
            raise ValueError("owner_scope and device_id are required")
        entry = await self._directory.get(owner_scope=owner_scope, device_id=device_id)
        if entry is None:
            raise KeyError(device_id)
        return entry
