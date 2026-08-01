"""In-memory hot projections layered over durable repositories."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from hub.domain.devices.entities import DeviceDirectoryEntry

logger = logging.getLogger(__name__)


class DirectorySnapshotSource(Protocol):
    async def get(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry | None: ...
    async def upsert(self, entry: DeviceDirectoryEntry) -> DeviceDirectoryEntry: ...
    async def list(self, *, owner_scope: str) -> tuple[DeviceDirectoryEntry, ...]: ...
    async def list_all(self) -> tuple[DeviceDirectoryEntry, ...]: ...


class CachedDeviceDirectoryRepository:
    """Write-through public directory cache.

    The database is authoritative.  Writes commit there before becoming visible
    in memory, startup hydrates from it, and periodic replacement reconciles
    changes made by other cloud instances.
    """

    def __init__(
        self,
        source: DirectorySnapshotSource,
        *,
        reconciliation_seconds: float = 5.0,
    ) -> None:
        if reconciliation_seconds <= 0:
            raise ValueError("reconciliation_seconds must be positive")
        self._source = source
        self._reconciliation_seconds = reconciliation_seconds
        self._values: dict[str, DeviceDirectoryEntry] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.refresh()
        if self._task is None:
            self._task = asyncio.create_task(
                self._reconcile_loop(),
                name="hub-device-directory-reconciler",
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def refresh(self) -> None:
        entries = await self._source.list_all()
        async with self._lock:
            self._values = {entry.device_id: entry for entry in entries}

    async def get(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry | None:
        async with self._lock:
            entry = self._values.get(device_id)
            return entry if entry is not None and entry.owner_scope == owner_scope else None

    async def upsert(self, entry: DeviceDirectoryEntry) -> DeviceDirectoryEntry:
        persisted = await self._source.upsert(entry)
        async with self._lock:
            self._values[persisted.device_id] = persisted
        return persisted

    async def list(self, *, owner_scope: str) -> tuple[DeviceDirectoryEntry, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (item for item in self._values.values() if item.owner_scope == owner_scope),
                    key=lambda item: item.device_id,
                )
            )

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reconciliation_seconds)
            try:
                await self.refresh()
            except Exception:
                logger.exception("Device Directory reconciliation failed")
