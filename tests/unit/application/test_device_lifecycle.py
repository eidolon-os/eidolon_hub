from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 4, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1"),
            enrollment_id="enrollment-1",
            retrieval_token_hash="a" * 64,
            retrieval_expires_at=NOW + timedelta(minutes=10),
            display_name="Generic Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
            enrolled_at=NOW - timedelta(minutes=1),
            updated_at=NOW - timedelta(minutes=1),
        )

    async def get(self, device_id):
        return self.device if device_id == "device-1" else None

    async def upsert(self, device):
        self.device = device
        return device


class _Recorder:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)

    async def execute(self, device_id):
        self.values.append(device_id)


class _Provider:
    def __init__(self, *, failures=0):
        self.revocations = []
        self.failures = failures

    async def revoke_channels(self, revocation):
        self.revocations.append(revocation)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("provider unavailable")


def _approve(devices, events=None, projector=None):
    return ApproveDevice(
        devices=devices,
        events=events or _Recorder(),
        clock=_Clock(),
        handoff_ttl=timedelta(minutes=30),
        directory_projector=projector or _Recorder(),
    )


async def test_approval_is_owner_scoped_and_extends_handoff_window_once() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    use_case = _approve(devices, events, projector)

    approved = await use_case.execute(
        device_id="device-1", owner_id="owner-1", request_id="approval-1"
    )
    replay = await use_case.execute(
        device_id="device-1", owner_id="owner-1", request_id="approval-1"
    )

    assert approved is replay
    assert approved.lifecycle_state is DeviceLifecycleState.APPROVED
    assert approved.retrieval_expires_at == NOW + timedelta(minutes=30)
    assert len(events.values) == 1
    assert projector.values == ["device-1"]
    with pytest.raises(ValueError, match="reused"):
        await use_case.execute(
            device_id="device-1", owner_id="owner-2", request_id="approval-1"
        )


async def test_expired_pending_or_revoked_device_cannot_be_approved() -> None:
    devices = _Devices()
    devices.device = replace(devices.device, retrieval_expires_at=NOW)
    with pytest.raises(ValueError, match="expired"):
        await _approve(devices).execute(
            device_id="device-1", owner_id="owner-1", request_id="approval-1"
        )
    devices.device = replace(devices.device, lifecycle_state=DeviceLifecycleState.REVOKED)
    with pytest.raises(ValueError, match="revoked"):
        await _approve(devices).execute(
            device_id="device-1", owner_id="owner-1", request_id="approval-2"
        )


async def test_approved_owner_cannot_be_replaced() -> None:
    devices = _Devices()
    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    with pytest.raises(ValueError, match="cannot be replaced"):
        await _approve(devices).execute(
            device_id="device-1", owner_id="owner-2", request_id="approval-2"
        )


async def test_revocation_notifies_provider_without_hub_session_state() -> None:
    devices, events, projector, provider = _Devices(), _Recorder(), _Recorder(), _Provider()
    devices.device = await _approve(devices, events, projector).execute(
        device_id="device-1", owner_id="owner-1", request_id="approval-1"
    )
    revoked = await RevokeDevice(
        devices=devices,
        provider=provider,
        hub_id="hub-local",
        events=events,
        clock=_Clock(),
        directory_projector=projector,
    ).execute(device_id="device-1", reason="operator-request", request_id="revoke-1")

    assert revoked.lifecycle_state is DeviceLifecycleState.REVOKED
    assert provider.revocations[0].device_id == "device-1"
    assert projector.values[-1] == "device-1"


async def test_provider_outage_keeps_revoked_state_and_same_request_retries() -> None:
    devices, provider = _Devices(), _Provider(failures=1)
    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    use_case = RevokeDevice(
        devices=devices,
        provider=provider,
        hub_id="hub-local",
        events=_Recorder(),
        clock=_Clock(),
        directory_projector=_Recorder(),
    )

    with pytest.raises(ConnectionError, match="unavailable"):
        await use_case.execute(
            device_id="device-1", reason="compromised", request_id="revoke-retry-1"
        )
    assert devices.device.lifecycle_state is DeviceLifecycleState.REVOKED
    replay = await use_case.execute(
        device_id="device-1", reason="compromised", request_id="revoke-retry-1"
    )
    assert replay.lifecycle_state is DeviceLifecycleState.REVOKED
    assert len(provider.revocations) == 2
