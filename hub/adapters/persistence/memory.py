"""In-memory hot device directory rebuilt from durable device facts."""

from __future__ import annotations

import asyncio

from hub.domain.devices.entities import DeviceDirectoryEntry


class InMemoryDeviceDirectoryRepository:
    """Single-process directory hot path; SQLite device facts remain authoritative."""

    def __init__(self) -> None:
        self._values: dict[str, DeviceDirectoryEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry | None:
        async with self._lock:
            entry = self._values.get(device_id)
            return entry if entry is not None and entry.owner_scope == owner_scope else None

    async def upsert(self, entry: DeviceDirectoryEntry) -> DeviceDirectoryEntry:
        async with self._lock:
            self._values[entry.device_id] = entry
        return entry

    async def list(self, *, owner_scope: str) -> tuple[DeviceDirectoryEntry, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (item for item in self._values.values() if item.owner_scope == owner_scope),
                    key=lambda item: item.device_id,
                )
            )
