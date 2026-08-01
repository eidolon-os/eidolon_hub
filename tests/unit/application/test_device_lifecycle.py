from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity(
                device_id="device-1",
                public_key_fingerprint="p256:fingerprint",
                tenant_id="local",
            ),
            display_name="Generic Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
            registered_at=NOW - timedelta(minutes=1),
            updated_at=NOW - timedelta(minutes=1),
        )

    async def get(self, device_id):
        return self.device if device_id == "device-1" else None

    async def upsert(self, device):
        self.device = device
        return device


class _Connections:
    def __init__(self):
        self.lease = ConnectionLease(
            connection_id="connection-1",
            device_id="device-1",
            connector_id="https-local",
            connector_kind=ConnectorKind.HTTPS,
            signaling_ref="http-mailbox:device-1",
            opened_at=NOW - timedelta(seconds=5),
            renewed_at=NOW - timedelta(seconds=5),
            expires_at=NOW + timedelta(seconds=45),
            lease_token="signed-lease-token-device-1",
            identity_fingerprint="p256:fingerprint",
            hub_instance_id="hub-local-1",
            fencing_token=1,
        )

    async def active_for_device(self, device_id, *, now):
        return (self.lease,) if self.lease.is_active(now) else ()

    async def upsert(self, lease):
        self.lease = lease
        return lease


class _Recorder:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)


class _Projector:
    def __init__(self):
        self.values = []

    async def execute(self, device_id):
        self.values.append(device_id)


async def test_approval_is_owner_scoped_and_request_id_idempotent() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Projector()
    use_case = ApproveDevice(
        devices=devices,
        events=events,
        clock=_Clock(),
        directory_projector=projector,
    )

    approved = await use_case.execute(
        device_id="device-1", owner_id="owner-1", request_id="approval-1"
    )
    replay = await use_case.execute(
        device_id="device-1", owner_id="owner-1", request_id="approval-1"
    )

    assert approved is replay
    assert approved.approved is True
    assert approved.owner_id == "owner-1"
    assert len(events.values) == 1
    assert projector.values == ["device-1"]
    with pytest.raises(ValueError, match="reused"):
        await use_case.execute(device_id="device-1", owner_id="owner-2", request_id="approval-1")


async def test_revocation_closes_only_hub_owned_connection_facts() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Projector()
    devices.device = await ApproveDevice(
        devices=devices,
        events=events,
        clock=_Clock(),
        directory_projector=projector,
    ).execute(device_id="device-1", owner_id="owner-1", request_id="approval-1")
    connections = _Connections()
    use_case = RevokeDevice(
        devices=devices,
        connections=connections,
        events=events,
        clock=_Clock(),
        directory_projector=projector,
    )

    revoked = await use_case.execute(
        device_id="device-1", reason="credential-compromise", request_id="revoke-1"
    )

    assert revoked.revoked is True
    assert revoked.approved is False
    assert connections.lease.is_active(NOW) is False
    assert projector.values[-1] == "device-1"
