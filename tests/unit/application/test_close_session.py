from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hub.application.use_cases.close_session import CloseDeviceSession
from hub.domain.sessions.entities import DeviceSessionLease, DeviceSessionState

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _session() -> DeviceSessionLease:
    return DeviceSessionLease(
        session_id="session-1",
        device_id="device-1",
        opened_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=45),
        lease_token="signed-session-token-device-1",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=1,
    )


class _Sessions:
    def __init__(self):
        self.item = _session()

    async def get(self, session_id):
        return self.item if session_id == self.item.session_id else None

    async def upsert(self, session):
        self.item = session
        return session


class _Events:
    def __init__(self):
        self.items = []

    async def publish(self, event):
        self.items.append(event)


class _Clock:
    def now(self):
        return NOW


async def test_session_close_is_durable_and_idempotent() -> None:
    sessions, events = _Sessions(), _Events()
    use_case = CloseDeviceSession(sessions=sessions, events=events, clock=_Clock())

    first = await use_case.execute(
        session_id="session-1",
        device_id="device-1",
        lease_token="signed-session-token-device-1",
    )
    retry = await use_case.execute(
        session_id="session-1",
        device_id="device-1",
        lease_token="signed-session-token-device-1",
    )

    assert first.state is DeviceSessionState.CLOSED
    assert retry == first == sessions.item
    assert {event.event_id for event in events.items} == {"session-1:closed"}
