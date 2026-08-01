"""Connection heartbeat/lease renewal shared by HTTPS and MQTT bindings."""

from __future__ import annotations

import hmac
from datetime import timedelta

from hub.domain.connections.entities import ConnectionLease
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import (
    ConnectionRepository,
    DeviceAuthorityRepository,
    DeviceDirectoryProjector,
)


class RenewConnection:
    def __init__(
        self,
        *,
        connections: ConnectionRepository,
        authority: DeviceAuthorityRepository,
        events: EventBus,
        clock: Clock,
        ttl: timedelta,
        directory_projector: DeviceDirectoryProjector | None = None,
    ) -> None:
        self._connections = connections
        self._authority = authority
        self._events = events
        self._clock = clock
        self._ttl = ttl
        self._directory_projector = directory_projector

    async def execute(
        self, *, connection_id: str, lease_token: str, sequence: int
    ) -> ConnectionLease:
        lease = await self._connections.get(connection_id)
        if lease is None:
            raise KeyError(connection_id)
        if not hmac.compare_digest(lease.lease_token, lease_token):
            raise PermissionError("connection lease token mismatch")
        now = self._clock.now()
        renewed = lease.renew(
            now=now,
            ttl=self._ttl,
            lease_token=lease.lease_token,
            sequence=sequence,
        )
        if renewed is lease:
            await self._authority.validate(
                device_id=lease.device_id,
                hub_instance_id=lease.hub_instance_id,
                fencing_token=lease.fencing_token,
                now=now,
            )
            return lease
        await self._authority.renew(
            device_id=lease.device_id,
            hub_instance_id=lease.hub_instance_id,
            fencing_token=lease.fencing_token,
            now=now,
            ttl=self._ttl,
        )
        await self._connections.upsert(renewed)
        await self._events.publish(
            DomainEvent(
                event_id=f"{connection_id}:{int(now.timestamp() * 1000)}",
                event_type="eidolon.connection.renewed.v1",
                source="eidolon-hub/connection-plane",
                subject=lease.device_id,
                occurred_at=now,
                data_json="{}",
            )
        )
        if self._directory_projector is not None:
            await self._directory_projector.execute(lease.device_id)
        return renewed
