"""Idempotently close one authenticated device session."""

from __future__ import annotations

import hmac

from hub.domain.sessions.entities import DeviceSessionLease, DeviceSessionState
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import DeviceDirectoryProjector, DeviceSessionRepository


class CloseDeviceSession:
    def __init__(
        self,
        *,
        sessions: DeviceSessionRepository,
        events: EventBus,
        clock: Clock,
        directory_projector: DeviceDirectoryProjector | None = None,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._clock = clock
        self._directory_projector = directory_projector

    async def execute(
        self, *, session_id: str, device_id: str, lease_token: str
    ) -> DeviceSessionLease:
        lease = await self._sessions.get(session_id)
        if lease is None:
            raise KeyError(session_id)
        if lease.device_id != device_id or not hmac.compare_digest(lease.lease_token, lease_token):
            raise PermissionError("device session does not match close request")
        now = self._clock.now()
        if lease.state is DeviceSessionState.CLOSED:
            closed = lease
        else:
            closed = lease.close()
            await self._sessions.upsert(closed)
        await self._events.publish(
            DomainEvent(
                event_id=f"{session_id}:closed",
                event_type="eidolon.device-session.closed.v1",
                source="eidolon-hub/device-access",
                subject=lease.device_id,
                occurred_at=now,
                data_json="{}",
            )
        )
        if self._directory_projector is not None:
            await self._directory_projector.execute(lease.device_id)
        return closed
