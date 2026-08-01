"""Authenticate an operation against one active connection lease."""

from __future__ import annotations

import hmac

from hub.domain.connections.entities import ConnectionLease
from hub.ports.identity import Clock
from hub.ports.repositories import ConnectionRepository


class AuthenticateConnection:
    def __init__(self, *, connections: ConnectionRepository, clock: Clock) -> None:
        self._connections = connections
        self._clock = clock

    async def execute(
        self, *, connection_id: str, device_id: str, lease_token: str
    ) -> ConnectionLease:
        lease = await self._connections.get(connection_id)
        if (
            lease is None
            or lease.device_id != device_id
            or not hmac.compare_digest(lease.lease_token, lease_token)
            or not lease.is_active(self._clock.now())
        ):
            raise PermissionError("active connection lease required")
        return lease
