from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hub.application.use_cases.close_connection import CloseConnection
from hub.domain.connections.entities import ConnectionLease, ConnectionState, ConnectorKind

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _lease(connection_id="connection-1"):
    return ConnectionLease(
        connection_id=connection_id,
        device_id="device-1",
        connector_id="mqtt-cloud",
        connector_kind=ConnectorKind.MQTT5,
        signaling_ref="mqtt:device-1",
        opened_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=45),
        lease_token="signed-lease-token-device-1",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=1,
    )


class _Connections:
    def __init__(self, *, with_alternative=False):
        self.items = {"connection-1": _lease()}
        if with_alternative:
            self.items["connection-2"] = _lease("connection-2")

    async def get(self, connection_id):
        return self.items.get(connection_id)

    async def upsert(self, lease):
        self.items[lease.connection_id] = lease
        return lease

    async def active_for_device(self, device_id, *, now):
        return tuple(
            item
            for item in self.items.values()
            if item.device_id == device_id and item.is_active(now)
        )


class _Events:
    def __init__(self):
        self.items = []

    async def publish(self, event):
        self.items.append(event)


class _Clock:
    def now(self):
        return NOW


def _use_case(*, alternative=False):
    connections, events = _Connections(with_alternative=alternative), _Events()
    use_case = CloseConnection(
        connections=connections,
        events=events,
        clock=_Clock(),
    )
    return use_case, connections, events


async def test_connection_close_persists_only_hub_owned_connection_fact() -> None:
    use_case, connections, events = _use_case()

    closed = await use_case.execute(
        connection_id="connection-1",
        device_id="device-1",
        lease_token="signed-lease-token-device-1",
    )

    assert closed.state is ConnectionState.CLOSED
    assert events.items[0].event_id == "connection-1:closed"
    assert connections.items["connection-1"] == closed


async def test_close_does_not_mutate_another_connector() -> None:
    use_case, connections, _events = _use_case(alternative=True)

    await use_case.execute(
        connection_id="connection-1",
        device_id="device-1",
        lease_token="signed-lease-token-device-1",
    )

    assert connections.items["connection-2"].state is ConnectionState.ACTIVE
