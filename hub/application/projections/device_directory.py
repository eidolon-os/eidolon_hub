"""Project durable device facts and active connections into the public blackboard."""

from __future__ import annotations

from hub.domain.devices.entities import DeviceDirectoryEntry, DirectoryConnection
from hub.ports.identity import Clock
from hub.ports.repositories import (
    ConnectionRepository,
    DeviceDirectoryRepository,
    DeviceRepository,
)


class ProjectDeviceDirectory:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        connections: ConnectionRepository,
        directory: DeviceDirectoryRepository,
        clock: Clock,
    ) -> None:
        self._devices = devices
        self._connections = connections
        self._directory = directory
        self._clock = clock

    async def execute(self, device_id: str) -> DeviceDirectoryEntry:
        device = await self._devices.get(device_id)
        if device is None:
            raise KeyError(device_id)
        now = self._clock.now()
        leases = await self._connections.active_for_device(device_id, now=now)
        owner_scope = device.owner_id or "unclaimed"
        entry = DeviceDirectoryEntry(
            device_id=device_id,
            owner_scope=owner_scope,
            display_name=device.display_name,
            device_kind=device.device_kind,
            manifest_json=device.manifest_json,
            manifest_revision=device.manifest_revision,
            approved=device.approved,
            revoked=device.revoked,
            online=bool(leases) and not device.revoked,
            connections=tuple(
                DirectoryConnection(
                    connection_id=lease.connection_id,
                    connector_id=lease.connector_id,
                    connector_kind=lease.connector_kind.value,
                    expires_at=lease.expires_at,
                )
                for lease in leases
            ),
            registered_at=device.registered_at,
            updated_at=now,
            revision=1,
        )
        # No signaling reference, credential, provider name, channel address or
        # opaque binding enters this durable projection.
        return await self._directory.upsert(entry)

    async def execute_all(self) -> tuple[DeviceDirectoryEntry, ...]:
        """Recompute time-dependent online facts from durable authorities."""

        projected: list[DeviceDirectoryEntry] = []
        for device in await self._devices.list_all():
            projected.append(await self.execute(device.identity.device_id))
        return tuple(projected)
