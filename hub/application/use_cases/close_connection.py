"""Idempotently close one return path without affecting other connectors."""

from __future__ import annotations

import hmac

from hub.domain.connections.entities import ConnectionLease, ConnectionState
from hub.ports.channels import DeviceChannelRevoker
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import ConnectionRepository, DeviceDirectoryProjector


class CloseConnection:
    def __init__(
        self,
        *,
        connections: ConnectionRepository,
        events: EventBus,
        clock: Clock,
        channel_revoker: DeviceChannelRevoker,
        directory_projector: DeviceDirectoryProjector | None = None,
    ) -> None:
        self._connections = connections
        self._events = events
        self._clock = clock
        self._channel_revoker = channel_revoker
        self._directory_projector = directory_projector

    async def execute(
        self, *, connection_id: str, device_id: str, lease_token: str
    ) -> ConnectionLease:
        lease = await self._connections.get(connection_id)
        if lease is None:
            raise KeyError(connection_id)
        if lease.device_id != device_id or not hmac.compare_digest(lease.lease_token, lease_token):
            raise PermissionError("connection lease does not match close request")
        now = self._clock.now()
        if lease.state is ConnectionState.CLOSED:
            closed = lease
        else:
            closed = lease.close()
            await self._connections.upsert(closed)
        if not await self._connections.active_for_device(lease.device_id, now=now):
            await self._channel_revoker.execute(lease.device_id, reason="connection-closed")
        await self._events.publish(
            DomainEvent(
                event_id=f"{connection_id}:closed",
                event_type="eidolon.connection.closed.v1",
                source="eidolon-hub/connection-plane",
                subject=lease.device_id,
                occurred_at=now,
                data_json="{}",
            )
        )
        if self._directory_projector is not None:
            await self._directory_projector.execute(lease.device_id)
        return closed
