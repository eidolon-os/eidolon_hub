from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.domain.devices.entities import DeviceEnrollmentIntent, DeviceLifecycleState
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 4, tzinfo=UTC)
TOKEN = "device-generated-random-token-000001"


class _Clock:
    value = NOW

    def now(self):
        return self.value


class _Ids:
    count = 0

    def new(self, prefix):
        self.count += 1
        return f"{prefix}-{self.count}"


class _Devices:
    def __init__(self):
        self.values = {}

    async def get(self, device_id):
        return self.values.get(device_id)

class _Recorder:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)

    async def execute(self, value):
        self.values.append(value)


class _Mutations:
    def __init__(self, devices, events):
        self.devices = devices
        self.events = events

    async def commit(self, *, expected, device, event):
        assert self.devices.values.get(device.identity.device_id) == expected
        self.devices.values[device.identity.device_id] = device
        self.events.values.append(event)
        return device


class _FailOnceProjector(_Recorder):
    failures = 1

    async def execute(self, value):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("projection failed")
        await super().execute(value)


def _intent(*, request_id="enroll-1", title="Device", token=TOKEN):
    return DeviceEnrollmentIntent(
        request_id=request_id,
        retrieval_token=token,
        identity=DeviceIdentity("device-1"),
        display_name=title,
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": title}
        ),
    )


def _use_case(devices, events, projector, clock=None):
    return EnrollDevice(
        devices=devices,
        mutations=_Mutations(devices, events),
        clock=clock or _Clock(),
        ids=_Ids(),
        tokens=Sha256RetrievalTokenHasher(),
        enrollment_ttl=timedelta(minutes=30),
        directory_projector=projector,
    )


async def test_enrollment_is_idempotent_and_request_id_is_content_bound() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    use_case = _use_case(devices, events, projector)

    first = await use_case.execute(_intent())
    retried = await use_case.execute(_intent())

    assert retried == first
    assert len(events.values) == 1
    assert projector.values == ["device-1", "device-1"]
    assert first.retrieval_token_hash != TOKEN
    with pytest.raises(ValueError, match="different content"):
        await use_case.execute(_intent(title="Changed"))


async def test_unexpired_or_approved_device_cannot_be_enrolled_again() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    use_case = _use_case(devices, events, projector)
    await use_case.execute(_intent())

    with pytest.raises(ValueError, match="already enrolled"):
        await use_case.execute(_intent(request_id="enroll-2", token="x" * 32))


async def test_revoked_device_can_enroll_again_and_needs_approval_again() -> None:
    # Removing a phone has to leave a way back — a reinstall, a recovered
    # handset — and the way back is a fresh enrollment the owner approves.
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    use_case = _use_case(devices, events, projector)
    first = await use_case.execute(_intent())
    devices.values["device-1"] = replace(
        first,
        lifecycle_state=DeviceLifecycleState.REVOKED,
        owner_id="owner-1",
    )

    again = await use_case.execute(
        _intent(request_id="enroll-2", token="new-device-random-retrieval-token-2")
    )

    assert again.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
    assert again.owner_id is None
    assert again.enrollment_id != first.enrollment_id


async def test_expired_pending_enrollment_can_restart_without_compatibility_state() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    clock = _Clock()
    use_case = _use_case(devices, events, projector, clock)
    first = await use_case.execute(_intent())
    clock.value = NOW + timedelta(minutes=31)

    restarted = await use_case.execute(
        _intent(request_id="enroll-2", token="new-device-random-retrieval-token-2")
    )

    assert restarted.enrollment_id != first.enrollment_id
    assert restarted.enrolled_at == clock.value
    assert len(events.values) == 2


async def test_idempotent_retry_detects_corrupt_fingerprint_metadata() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    use_case = _use_case(devices, events, projector)
    device = await use_case.execute(_intent())
    devices.values["device-1"] = replace(device, last_enrollment_fingerprint="bad")

    with pytest.raises(ValueError, match="different content"):
        await use_case.execute(_intent())


async def test_retry_repairs_projection_without_duplicating_audit() -> None:
    devices, events, projector = _Devices(), _Recorder(), _FailOnceProjector()
    use_case = _use_case(devices, events, projector)

    with pytest.raises(RuntimeError, match="projection failed"):
        await use_case.execute(_intent())
    repaired = await use_case.execute(_intent())

    assert repaired.identity.device_id == "device-1"
    assert len(events.values) == 1
    assert projector.values == ["device-1"]
