from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.idempotency import mutation_fingerprint
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 4, tzinfo=UTC)
PRINCIPAL = "owner-operator"


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self, device=None):
        self.device = device

    async def get(self, device_id):
        return self.device if self.device and self.device.identity.device_id == device_id else None

    async def commit(self, *, expected, device, event):
        assert self.device == expected
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
        mutations=devices,
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
            device_id="device-1",
            owner_id="",
            request_id="request-1",
            principal_id=PRINCIPAL,
        )
    with pytest.raises(KeyError):
        await _approve(_Devices()).execute(
            device_id="missing",
            owner_id="owner-1",
            request_id="request-1",
            principal_id=PRINCIPAL,
        )
    with pytest.raises(ValueError, match="revoked"):
        await _approve(_Devices(_device(lifecycle_state=DeviceLifecycleState.REVOKED))).execute(
            device_id="device-1",
            owner_id="owner-1",
            request_id="request-1",
            principal_id=PRINCIPAL,
        )


async def test_revoke_missing_and_replay_metadata_guards() -> None:
    devices, provider = _Devices(), _Provider()
    revoke = RevokeDevice(
        devices=devices,
        provider=provider,
        hub_id="hub-1",
        mutations=devices,
        clock=_Clock(),
        directory_projector=_Recorder(),
    )
    with pytest.raises(KeyError):
        await revoke.execute(
            owner_scope=None,
            device_id="missing",
            reason="test",
            request_id="revoke-1",
            principal_id=PRINCIPAL,
        )

    devices.device = _device(
        lifecycle_state=DeviceLifecycleState.REVOKED,
        last_management_request_id="revoke-1",
        last_management_fingerprint=mutation_fingerprint(
            "device.revoke", {"reason": "test", "principal_id": PRINCIPAL}
        ),
    )
    replay = await revoke.execute(
        owner_scope=None,
            device_id="device-1",
        reason="test",
        request_id="revoke-1",
        principal_id=PRINCIPAL,
    )
    assert replay.lifecycle_state is DeviceLifecycleState.REVOKED
    assert len(provider.revocations) == 1
    with pytest.raises(ValueError, match="reused"):
        await revoke.execute(
            owner_scope=None,
            device_id="device-1",
            reason="other",
            request_id="revoke-1",
            principal_id=PRINCIPAL,
        )

    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    with pytest.raises(RuntimeError, match="inconsistent"):
        await revoke.execute(
            owner_scope=None,
            device_id="device-1",
            reason="test",
            request_id="revoke-1",
            principal_id=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_revoking_names_an_owner_and_the_hub_holds_it_to_that() -> None:
    """The Hub is where "whose device is this" is actually known.

    Its use case took an identifier and revoked whatever it named, so every
    layer above could reasonably assume some other layer had checked — and
    none did. The caller now states the owner, and this record refuses a
    mutation that names one who does not hold it.

    That is not the boundary's authorization check made twice: the boundary
    decides whether a session speaks for an Owner, and this decides whether
    its own record agrees. Different questions, different places.
    """

    devices = _Devices()
    devices.device = _device(
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    revoke = RevokeDevice(
        devices=devices,
        provider=_Provider(),
        hub_id="hub-local",
        mutations=devices,
        clock=_Clock(),
        directory_projector=_Recorder(),
    )

    with pytest.raises(PermissionError):
        await revoke.execute(
            device_id="device-1",
            owner_scope="owner-somebody-else",
            reason="test",
            request_id="revoke-1",
            principal_id=PRINCIPAL,
        )

    # Still revocable by the owner who has it, and by an operator who is
    # withdrawing without naming one.
    revoked = await revoke.execute(
        device_id="device-1",
        owner_scope="owner-1",
        reason="test",
        request_id="revoke-1",
        principal_id=PRINCIPAL,
    )
    assert revoked.lifecycle_state is DeviceLifecycleState.REVOKED
