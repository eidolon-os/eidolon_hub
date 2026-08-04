from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.projections.device_directory import ProjectDeviceDirectory
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
    def __init__(self, device=None):
        self.device = device

    async def get(self, device_id):
        return self.device if self.device and self.device.identity.device_id == device_id else None

    async def upsert(self, device):
        self.device = device
        return device

    async def list_all(self):
        return (self.device,) if self.device else ()


class _Recorder:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)

    async def execute(self, value):
        self.values.append(value)

    async def upsert(self, value):
        self.values.append(value)
        return value


class _Provider:
    def __init__(self):
        self.revocations = []

    async def revoke_channels(self, revocation):
        self.revocations.append(revocation)


def _device(**changes):
    values = {
        "identity": DeviceIdentity("device-1"),
        "enrollment_id": "enrollment-1",
        "retrieval_token_hash": "a" * 64,
        "retrieval_expires_at": NOW + timedelta(minutes=30),
        "display_name": "Device",
        "device_kind": "generic",
        "manifest": DeviceManifestDocument.from_mapping({"schema_version": 1}),
        "enrolled_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ManagedDevice(**values)


def _approve(devices):
    return ApproveDevice(
        devices=devices,
        events=_Recorder(),
        clock=_Clock(),
        handoff_ttl=timedelta(minutes=30),
        directory_projector=_Recorder(),
    )


async def test_directory_missing_device_is_explicit() -> None:
    with pytest.raises(KeyError):
        await ProjectDeviceDirectory(devices=_Devices(), directory=_Recorder()).execute("missing")


async def test_approval_rejects_invalid_owner_missing_device_and_revoked_device() -> None:
    with pytest.raises(ValueError, match="owner_id"):
        await _approve(_Devices()).execute(
            device_id="device-1", owner_id="", request_id="request-1"
        )
    with pytest.raises(KeyError):
        await _approve(_Devices()).execute(
            device_id="missing", owner_id="owner-1", request_id="request-1"
        )
    with pytest.raises(ValueError, match="revoked"):
        await _approve(_Devices(_device(lifecycle_state=DeviceLifecycleState.REVOKED))).execute(
            device_id="device-1", owner_id="owner-1", request_id="request-1"
        )


async def test_revoke_missing_and_replay_metadata_guards() -> None:
    devices, provider = _Devices(), _Provider()
    revoke = RevokeDevice(
        devices=devices,
        provider=provider,
        hub_id="hub-1",
        events=_Recorder(),
        clock=_Clock(),
        directory_projector=_Recorder(),
    )
    with pytest.raises(KeyError):
        await revoke.execute(device_id="missing", reason="test", request_id="revoke-1")

    devices.device = _device(
        lifecycle_state=DeviceLifecycleState.REVOKED,
        last_management_request_id="revoke-1",
        last_management_fingerprint="revoke:test",
    )
    replay = await revoke.execute(device_id="device-1", reason="test", request_id="revoke-1")
    assert replay.lifecycle_state is DeviceLifecycleState.REVOKED
    assert len(provider.revocations) == 1
    with pytest.raises(ValueError, match="reused"):
        await revoke.execute(device_id="device-1", reason="other", request_id="revoke-1")

    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    with pytest.raises(RuntimeError, match="inconsistent"):
        await revoke.execute(device_id="device-1", reason="test", request_id="revoke-1")
