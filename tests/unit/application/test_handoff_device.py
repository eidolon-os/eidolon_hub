from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.application.use_cases.handoff_device import HandoffDevice
from hub.domain.channels.entities import ChannelAssignmentSet
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 4, tzinfo=UTC)
TOKEN = "device-generated-random-token-000001"
TOKENS = Sha256RetrievalTokenHasher()


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self, device):
        self.device = device

    async def get_by_enrollment_id(self, enrollment_id):
        return self.device if enrollment_id == self.device.enrollment_id else None


class _Provision:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        device = kwargs["device"]
        return ChannelAssignmentSet(
            operation_id=kwargs["operation_id"],
            device_id=device.identity.device_id,
            manifest_revision=device.manifest_revision,
            grants=(),
        )


def _device(*, state=DeviceLifecycleState.PENDING_APPROVAL):
    return ManagedDevice(
        identity=DeviceIdentity("device-1"),
        enrollment_id="enrollment-1",
        retrieval_token_hash=TOKENS.hash(TOKEN),
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Device"}
        ),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_id="owner-1" if state is DeviceLifecycleState.APPROVED else None,
        lifecycle_state=state,
    )


def _use_case(device, provision=None):
    provision = provision or _Provision()
    return HandoffDevice(
        devices=_Devices(device),
        provision=provision,
        tokens=TOKENS,
        clock=_Clock(),
    ), provision


async def test_pending_handoff_does_not_call_provider() -> None:
    use_case, provision = _use_case(_device())
    outcome = await use_case.execute(enrollment_id="enrollment-1", retrieval_token=TOKEN)

    assert outcome.device.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
    assert outcome.assignments is None
    assert provision.calls == []


async def test_approved_handoff_uses_stable_enrollment_operation_id() -> None:
    use_case, provision = _use_case(_device(state=DeviceLifecycleState.APPROVED))
    outcome = await use_case.execute(enrollment_id="enrollment-1", retrieval_token=TOKEN)

    assert outcome.assignments is not None
    assert provision.calls[0]["operation_id"] == "enrollment-1"


async def test_unknown_invalid_and_expired_handoffs_fail_closed() -> None:
    device = _device()
    use_case, _ = _use_case(device)
    with pytest.raises(KeyError):
        await use_case.execute(enrollment_id="missing", retrieval_token=TOKEN)
    with pytest.raises(PermissionError, match="invalid"):
        await use_case.execute(enrollment_id=device.enrollment_id, retrieval_token="wrong" * 8)

    expired, _ = _use_case(replace(device, retrieval_expires_at=NOW))
    with pytest.raises(TimeoutError, match="expired"):
        await expired.execute(enrollment_id=device.enrollment_id, retrieval_token=TOKEN)


async def test_revoked_handoff_returns_terminal_state_without_provider() -> None:
    use_case, provision = _use_case(_device(state=DeviceLifecycleState.REVOKED))
    outcome = await use_case.execute(enrollment_id="enrollment-1", retrieval_token=TOKEN)

    assert outcome.device.lifecycle_state is DeviceLifecycleState.REVOKED
    assert outcome.assignments is None
    assert provision.calls == []
