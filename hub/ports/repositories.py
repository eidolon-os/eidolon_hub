"""Small persistence ports exposed to application use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from hub.domain.channels.entities import ChannelLease
from hub.domain.commands.entities import DeviceCommand
from hub.domain.connections.entities import ConnectionLease, DeviceAuthorityLease
from hub.domain.devices.entities import DeviceDirectoryEntry, ManagedDevice


class DeviceRepository(Protocol):
    async def get(self, device_id: str) -> ManagedDevice | None: ...
    async def upsert(self, device: ManagedDevice) -> ManagedDevice: ...
    async def list_all(self) -> tuple[ManagedDevice, ...]: ...


class CommandRepository(Protocol):
    async def get(self, command_id: str) -> DeviceCommand | None: ...
    async def upsert(self, command: DeviceCommand) -> DeviceCommand: ...


class ConnectionRepository(Protocol):
    async def get(self, connection_id: str) -> ConnectionLease | None: ...
    async def upsert(self, lease: ConnectionLease) -> ConnectionLease: ...
    async def active_for_device(
        self, device_id: str, *, now: datetime
    ) -> tuple[ConnectionLease, ...]: ...


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
        self, device_id: str, *, now: datetime, profile_name: str | None = None
    ) -> tuple[ChannelLease, ...]: ...
    async def list_for_device(self, device_id: str) -> tuple[ChannelLease, ...]: ...


class ChannelCursorRepository(Protocol):
    async def next_outbound(self, channel_id: str) -> int: ...
    async def accept_inbound(self, *, channel_id: str, sequence: int, envelope_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class GuardBindingContext:
    binding_id: str
    owner_id: str
    device_id: str
    guard_companion_id: str
    policy_id: str
    policy_config_json: str
    runtime_config_json: str
    config_revision: int


class GuardRepository(Protocol):
    async def active_binding_for_device(self, device_id: str) -> GuardBindingContext | None: ...


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    event_id: str
    event_type: str
    owner_id: str
    subject_type: str
    subject_id: str
    occurred_at: datetime
    payload_json: str


class EventLedger(Protocol):
    async def append(self, event: LedgerEvent) -> None: ...
