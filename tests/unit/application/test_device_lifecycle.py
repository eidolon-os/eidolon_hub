from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.claim_lifecycle import ClaimCommandResult

NOW = datetime(2026, 8, 4, tzinfo=UTC)
PRINCIPAL = "owner-operator"


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


class _Recorder:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)

    async def execute(self, device_id):
        self.values.append(device_id)


class _Mutations:
    def __init__(self, devices, events):
        self.devices = devices
        self.events = events

    async def commit(self, *, expected, device, event):
        assert self.devices.device == expected
        self.devices.device = device
        self.events.values.append(event)
        return device


class _Ids:
    def new(self, prefix):
        return f"{prefix}-1"


class _Claims:
    def __init__(self, devices):
        self.devices = devices
        self.commands = {}
        self.events = []

    async def get_command(self, *, owner_domain_id, command_type, command_id):
        result = self.commands.get((owner_domain_id, command_type, command_id))
        return None if result is None else replace(result, outcome="replayed")

    async def commit_revoke(self, *, expected, revoked, command_id, fingerprint, event):
        assert self.devices.device == expected
        self.devices.device = revoked
        result = ClaimCommandResult(
            command_id=command_id,
            fingerprint=fingerprint,
            outcome="committed",
            device_ref=event.device_ref,
            aggregate_revision=event.aggregate_revision,
            occurred_at=event.occurred_at,
            event_id=event.event_id,
        )
        self.commands[(event.device_ref.owner_domain_id, "device.claim.revoke", command_id)] = result
        self.events.append(event)
        return result

    async def commit_terminal_result(
        self, *, device, command_id, fingerprint, occurred_at
    ):
        result = ClaimCommandResult(
            command_id=command_id,
            fingerprint=fingerprint,
            outcome="committed",
            device_ref=device.device_ref,
            aggregate_revision=device.aggregate_revision,
            occurred_at=occurred_at,
            event_id=None,
        )
        self.commands[(device.owner_id, "device.claim.revoke", command_id)] = result
        return result


def _approve(devices, events=None, projector=None):
    recorder = events or _Recorder()
    return ApproveDevice(
        devices=devices,
        mutations=_Mutations(devices, recorder),
        clock=_Clock(),
        handoff_ttl=timedelta(minutes=30),
        directory_projector=projector or _Recorder(),
    )


async def test_approval_is_owner_scoped_and_extends_handoff_window_once() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    use_case = _approve(devices, events, projector)

    approved = await use_case.execute(
        device_id="device-1",
        owner_id="owner-1",
        request_id="approval-1",
        principal_id=PRINCIPAL,
    )
    replay = await use_case.execute(
        device_id="device-1",
        owner_id="owner-1",
        request_id="approval-1",
        principal_id=PRINCIPAL,
    )

    assert approved is replay
    assert approved.lifecycle_state is DeviceLifecycleState.APPROVED
    assert approved.retrieval_expires_at == NOW + timedelta(minutes=30)
    assert len(events.values) == 1
    assert projector.values == ["device-1", "device-1"]
    with pytest.raises(ValueError, match="reused"):
        await use_case.execute(
            device_id="device-1",
            owner_id="owner-2",
            request_id="approval-1",
            principal_id=PRINCIPAL,
        )


async def test_approval_idempotency_fingerprint_has_no_delimiter_collisions() -> None:
    devices = _Devices()
    use_case = _approve(devices)

    await use_case.execute(
        device_id="device-1",
        owner_id="owner:a",
        request_id="approval-delimiter",
        principal_id="principal",
    )

    with pytest.raises(ValueError, match="reused"):
        await use_case.execute(
            device_id="device-1",
            owner_id="owner",
            request_id="approval-delimiter",
            principal_id="a:principal",
        )


async def test_expired_pending_or_revoked_device_cannot_be_approved() -> None:
    devices = _Devices()
    devices.device = replace(devices.device, retrieval_expires_at=NOW)
    with pytest.raises(ValueError, match="expired"):
        await _approve(devices).execute(
            device_id="device-1",
            owner_id="owner-1",
            request_id="approval-1",
            principal_id=PRINCIPAL,
        )
    devices.device = replace(devices.device, lifecycle_state=DeviceLifecycleState.REVOKED)
    with pytest.raises(ValueError, match="revoked"):
        await _approve(devices).execute(
            device_id="device-1",
            owner_id="owner-1",
            request_id="approval-2",
            principal_id=PRINCIPAL,
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
            device_id="device-1",
            owner_id="owner-2",
            request_id="approval-2",
            principal_id=PRINCIPAL,
        )


async def test_revocation_atomically_commits_claim_and_event_without_provider_dependency() -> None:
    devices, events, projector = _Devices(), _Recorder(), _Recorder()
    devices.device = await _approve(devices, events, projector).execute(
        device_id="device-1",
        owner_id="owner-1",
        request_id="approval-1",
        principal_id=PRINCIPAL,
    )
    claims = _Claims(devices)
    result = await RevokeDevice(
        devices=devices,
        claims=claims,
        clock=_Clock(),
        ids=_Ids(),
        directory_projector=projector,
    ).execute(
        device_ref=devices.device.device_ref,
        reason="operator-request",
        command_id="revoke-1",
        correlation_id="intent-1",
        principal_id=PRINCIPAL,
    )

    assert result.outcome == "committed"
    assert devices.device.lifecycle_state is DeviceLifecycleState.REVOKED
    assert claims.events[0].causation_id == "revoke-1"
    assert projector.values[-1] == "device-1"


async def test_revocation_command_replay_does_not_emit_a_second_event() -> None:
    devices = _Devices()
    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    claims = _Claims(devices)
    use_case = RevokeDevice(
        devices=devices,
        claims=claims,
        clock=_Clock(),
        ids=_Ids(),
        directory_projector=_Recorder(),
    )
    device_ref = devices.device.device_ref
    first = await use_case.execute(
        device_ref=device_ref,
        reason="compromised",
        command_id="revoke-retry-1",
        correlation_id="intent-1",
        principal_id=PRINCIPAL,
    )
    replay = await use_case.execute(
        device_ref=device_ref,
        reason="compromised",
        command_id="revoke-retry-1",
        correlation_id="intent-1",
        principal_id="another-auditor",
    )
    assert first.outcome == "committed"
    assert replay.outcome == "replayed"
    assert len(claims.events) == 1


async def test_revocation_idempotency_fingerprint_has_no_delimiter_collisions() -> None:
    devices = _Devices()
    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    claims = _Claims(devices)
    use_case = RevokeDevice(
        devices=devices,
        claims=claims,
        clock=_Clock(),
        ids=_Ids(),
        directory_projector=_Recorder(),
    )
    device_ref = devices.device.device_ref

    await use_case.execute(
        device_ref=device_ref,
        reason="reason:a",
        command_id="revoke-delimiter",
        correlation_id="intent-1",
        principal_id="principal",
    )

    with pytest.raises(ValueError, match="reused"):
        await use_case.execute(
            device_ref=device_ref,
            reason="reason",
            command_id="revoke-delimiter",
            correlation_id="intent-1",
            principal_id="a:principal",
        )


async def test_revocation_refuses_a_stale_claim_generation() -> None:
    devices = _Devices()
    devices.device = replace(
        devices.device,
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
        claim_generation=2,
    )
    use_case = RevokeDevice(
        devices=devices,
        claims=_Claims(devices),
        clock=_Clock(),
        ids=_Ids(),
        directory_projector=_Recorder(),
    )
    stale_ref = devices.device.device_ref.model_copy(
        update={"claim_generation": 1}
    )
    with pytest.raises(LookupError, match="stale"):
        await use_case.execute(
            device_ref=stale_ref,
            reason="owner-removed",
            command_id="stale-command",
            correlation_id="intent-stale",
            principal_id=PRINCIPAL,
        )
