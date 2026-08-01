from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.domain.connections.registry import ConnectionRegistry, StaleFencingToken


def _lease(
    connection_id: str,
    kind: ConnectorKind,
    *,
    now: datetime,
    priority: int,
    fencing_token: int = 1,
) -> ConnectionLease:
    return ConnectionLease(
        connection_id=connection_id,
        device_id="device-1",
        connector_id=f"connector-{kind.value}",
        connector_kind=kind,
        signaling_ref=f"{kind.value}:device-1",
        opened_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=45),
        lease_token=f"lease-token-{connection_id}",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=fencing_token,
        priority=priority,
    )


def test_online_is_aggregated_from_multiple_connection_leases() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    registry = ConnectionRegistry()
    registry.open(_lease("mqtt-1", ConnectorKind.MQTT5, now=now, priority=20))
    registry.open(_lease("https-1", ConnectorKind.HTTPS, now=now, priority=10))

    assert registry.is_online("device-1", now=now)
    assert registry.preferred_for_device("device-1", now=now).connection_id == "https-1"

    registry.close("https-1")
    assert registry.preferred_for_device("device-1", now=now).connection_id == "mqtt-1"
    registry.close("mqtt-1")
    assert not registry.is_online("device-1", now=now)


def test_expired_connection_does_not_make_device_online() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    registry = ConnectionRegistry()
    registry.open(_lease("mqtt-1", ConnectorKind.MQTT5, now=now, priority=10))

    assert not registry.is_online("device-1", now=now + timedelta(seconds=46))
    assert [item.connection_id for item in registry.expire(now=now + timedelta(seconds=46))] == [
        "mqtt-1"
    ]


def test_lower_fencing_token_cannot_replace_connection() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    registry = ConnectionRegistry()
    registry.open(_lease("mqtt-1", ConnectorKind.MQTT5, now=now, priority=10, fencing_token=2))

    with pytest.raises(StaleFencingToken):
        registry.open(_lease("mqtt-1", ConnectorKind.MQTT5, now=now, priority=10, fencing_token=1))
