"""Small persistence ports exposed to application use cases."""

from __future__ import annotations

from typing import Protocol

from hub.domain.devices.entities import DeviceDirectoryEntry, ManagedDevice
from hub.ports.management_events import DeviceManagementEventRecord


class ConcurrentDeviceMutationError(ValueError):
    """The authoritative device changed after the use case read it."""


class DeviceRepository(Protocol):
    async def get(self, device_id: str) -> ManagedDevice | None: ...
    async def list_all(self) -> tuple[ManagedDevice, ...]: ...


class DeviceMutationUnitOfWork(Protocol):
    """Atomically persist one device fact and its management audit event."""

    async def commit(
        self,
        *,
        expected: ManagedDevice | None,
        device: ManagedDevice,
        event: DeviceManagementEventRecord,
    ) -> ManagedDevice: ...


class DeviceDirectoryRepository(Protocol):
    async def get(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry | None: ...
    async def upsert(self, entry: DeviceDirectoryEntry) -> DeviceDirectoryEntry: ...
    async def list(self, *, owner_scope: str) -> tuple[DeviceDirectoryEntry, ...]: ...


class DeviceDirectoryProjector(Protocol):
    async def execute(self, device_id: str) -> DeviceDirectoryEntry: ...
