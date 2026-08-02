"""Renew an authenticated device session from one HTTPS heartbeat."""

from __future__ import annotations

import hmac
from datetime import timedelta

from hub.domain.sessions.entities import DeviceSessionLease
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import (
    DeviceAuthorityRepository,
    DeviceDirectoryProjector,
    DeviceSessionRepository,
)


class RenewDeviceSession:
    def __init__(
        self,
        *,
        sessions: DeviceSessionRepository,
        authority: DeviceAuthorityRepository,
        events: EventBus,
        clock: Clock,
        ttl: timedelta,
        directory_projector: DeviceDirectoryProjector | None = None,
    ) -> None:
        self._sessions = sessions
        self._authority = authority
        self._events = events
        self._clock = clock
        self._ttl = ttl
        self._directory_projector = directory_projector

    async def execute(
        self, *, session_id: str, lease_token: str, sequence: int
    ) -> DeviceSessionLease:
        lease = await self._sessions.get(session_id)
        if lease is None:
            raise KeyError(session_id)
        if not hmac.compare_digest(lease.lease_token, lease_token):
            raise PermissionError("session lease token mismatch")
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
        await self._sessions.upsert(renewed)
        await self._events.publish(
            DomainEvent(
                event_id=f"{session_id}:{int(now.timestamp() * 1000)}",
                event_type="eidolon.device-session.renewed.v1",
                source="eidolon-hub/device-access",
                subject=lease.device_id,
                occurred_at=now,
                data_json="{}",
            )
        )
        if self._directory_projector is not None:
            await self._directory_projector.execute(lease.device_id)
        return renewed
