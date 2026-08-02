"""Small persistence ports exposed to application use cases."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from hub.domain.channels.entities import ChannelLease
from hub.domain.commands.entities import DeviceCommand
from hub.domain.devices.entities import DeviceDirectoryEntry, ManagedDevice
from hub.domain.sessions.entities import DeviceAuthorityLease, DeviceSessionLease


class DeviceRepository(Protocol):
    async def get(self, device_id: str) -> ManagedDevice | None: ...
    async def upsert(self, device: ManagedDevice) -> ManagedDevice: ...
    async def list_all(self) -> tuple[ManagedDevice, ...]: ...


class CommandRepository(Protocol):
    async def get(self, command_id: str) -> DeviceCommand | None: ...
    async def upsert(self, command: DeviceCommand) -> DeviceCommand: ...


class DeviceSessionRepository(Protocol):
    async def get(self, session_id: str) -> DeviceSessionLease | None: ...
    async def upsert(self, lease: DeviceSessionLease) -> DeviceSessionLease: ...
    async def active_for_device(
        self, device_id: str, *, now: datetime
    ) -> tuple[DeviceSessionLease, ...]: ...


class DeviceAuthorityRepository(Protocol):
    async def validate(
        self,
        *,
        device_id: str,
        hub_instance_id: str,
        fencing_token: int,
        now: datetime,
    ) -> DeviceAuthorityLease: ...

    async def acquire(
        self,
        *,
        device_id: str,
        hub_instance_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> DeviceAuthorityLease: ...

    async def renew(
        self,
        *,
        device_id: str,
        hub_instance_id: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> DeviceAuthorityLease: ...


class DeviceDirectoryRepository(Protocol):
    async def get(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry | None: ...
    async def upsert(self, entry: DeviceDirectoryEntry) -> DeviceDirectoryEntry: ...
    async def list(self, *, owner_scope: str) -> tuple[DeviceDirectoryEntry, ...]: ...


class DeviceDirectoryProjector(Protocol):
    async def execute(self, device_id: str) -> DeviceDirectoryEntry: ...


class ChannelLeaseRepository(Protocol):
    async def get(self, channel_id: str) -> ChannelLease | None: ...
    async def upsert(self, lease: ChannelLease) -> ChannelLease: ...
    async def delete(self, channel_id: str) -> None: ...
    async def active_for_device(
        self, device_id: str, *, now: datetime, purpose: str | None = None
    ) -> tuple[ChannelLease, ...]: ...
    async def list_for_device(self, device_id: str) -> tuple[ChannelLease, ...]: ...


class ChannelCursorRepository(Protocol):
    async def next_outbound(self, channel_id: str) -> int: ...
    async def accept_inbound(self, *, channel_id: str, sequence: int, envelope_id: str) -> bool: ...
