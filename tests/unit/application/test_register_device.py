from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.register_device import RegisterDevice
from hub.contracts.bindings.common import DeviceIdentity as WireIdentity
from hub.contracts.bindings.device import (
    DeviceManifest,
    DeviceRegistration,
)
from hub.contracts.mappers import registration_to_domain
from hub.domain.connections.entities import ConnectionLease, ConnectorKind


class _Clock:
    value = datetime(2026, 8, 1, tzinfo=UTC)

    def now(self):
        return self.value


class _Devices:
    def __init__(self):
        self.values = {}

    async def get(self, device_id):
        return self.values.get(device_id)

    async def upsert(self, device):
        self.values[device.identity.device_id] = device
        return device


class _Connections:
    def __init__(self, lease):
        self.lease = lease

    async def get(self, connection_id):
        return self.lease if self.lease.connection_id == connection_id else None


class _Events:
    def __init__(self):
        self.values = []

    async def publish(self, event):
        self.values.append(event)


def _wire_registration(*, title="Device"):
    return DeviceRegistration(
        request_id="registration-request-1",
        identity=WireIdentity(
            device_id="device-1",
            public_key_fingerprint="p256:fingerprint",
            tenant_id="local",
        ),
        manifest=DeviceManifest(title=title),
        display_name=title,
        device_kind="generic",
    )


@pytest.mark.asyncio
async def test_registration_retry_is_idempotent_and_request_id_is_content_bound() -> None:
    now = _Clock.value
    lease = ConnectionLease(
        connection_id="connection-1",
        device_id="device-1",
        connector_id="https-local",
        connector_kind=ConnectorKind.HTTPS,
        signaling_ref="http-mailbox:device-1",
        opened_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=45),
        lease_token="lease-token-device-1",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=1,
    )
    devices = _Devices()
    events = _Events()
    use_case = RegisterDevice(
        devices=devices,
        connections=_Connections(lease),
        events=events,
        clock=_Clock(),
    )
    intent = registration_to_domain(_wire_registration())

    first = await use_case.execute(
        connection_id=lease.connection_id,
        lease_token=lease.lease_token,
        registration=intent,
    )
    retried = await use_case.execute(
        connection_id=lease.connection_id,
        lease_token=lease.lease_token,
        registration=intent,
    )

    assert retried == first
    assert len(events.values) == 1
    with pytest.raises(ValueError, match="different content"):
        await use_case.execute(
            connection_id=lease.connection_id,
            lease_token=lease.lease_token,
            registration=registration_to_domain(_wire_registration(title="Changed")),
        )
