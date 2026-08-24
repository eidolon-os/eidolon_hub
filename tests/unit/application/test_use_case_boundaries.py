from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from eidolon_sdk.device_foundation.v1 import OwnerDomainId

from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.rename_device import RenameDevice
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


class _Ids:
    def new(self, prefix):
        return f"{prefix}-1"


class _Claims:
    def __init__(self, devices):
        self.devices = devices
        self.commands = {}
        self.events = []

    async def get_command(self, *, owner_domain_id, command_type, command_id):
        value = self.commands.get((owner_domain_id, command_type, command_id))
        return None if value is None else replace(value, outcome="replayed")

    async def commit_revoke(self, *, expected, revoked, command_id, fingerprint, event):
        assert self.devices.device == expected
        self.devices.device = revoked
        value = ClaimCommandResult(
            command_id=command_id,
            fingerprint=fingerprint,
            outcome="committed",
            device_ref=event.device_ref,
            aggregate_revision=event.aggregate_revision,
            occurred_at=event.occurred_at,
            event_id=event.event_id,
        )
        self.commands[
            (str(event.device_ref.owner_domain_id), "device.claim.revoke", command_id)
        ] = value
        self.events.append(event)
        return value

    async def commit_terminal_result(self, *, device, command_id, fingerprint, occurred_at):
        value = ClaimCommandResult(
            command_id=command_id,
            fingerprint=fingerprint,
            outcome="committed",
            device_ref=device.device_ref,
            aggregate_revision=device.aggregate_revision,
            occurred_at=occurred_at,
            event_id=None,
        )
        self.commands[
            (str(device.device_ref.owner_domain_id), "device.claim.revoke", command_id)
        ] = value
        return value


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
    devices = _Devices()
    claims = _Claims(devices)
    revoke = RevokeDevice(
        devices=devices,
        claims=claims,
        clock=_Clock(),
        ids=_Ids(),
        directory_projector=_Recorder(),
    )
    missing_ref = _device(
        owner_id="owner-1", lifecycle_state=DeviceLifecycleState.APPROVED
    ).device_ref
    with pytest.raises(KeyError):
        await revoke.execute(
            device_ref=missing_ref.model_copy(update={"device_instance_id": "missing"}),
            reason="test",
            command_id="revoke-1",
            correlation_id="intent-1",
            principal_id=PRINCIPAL,
        )

    devices.device = _device(
        lifecycle_state=DeviceLifecycleState.REVOKED,
        owner_id="owner-1",
    )
    replay = await revoke.execute(
        device_ref=devices.device.device_ref,
        reason="test",
        command_id="revoke-1",
        correlation_id="intent-1",
        principal_id=PRINCIPAL,
    )
    assert replay.event_id is None
    assert claims.events == []
    with pytest.raises(ValueError, match="reused"):
        await revoke.execute(
            device_ref=devices.device.device_ref,
            reason="other",
            command_id="revoke-1",
            correlation_id="intent-1",
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
        claims=_Claims(devices),
        clock=_Clock(),
        ids=_Ids(),
        directory_projector=_Recorder(),
    )

    with pytest.raises(PermissionError):
        await revoke.execute(
            device_ref=devices.device.device_ref.model_copy(
                update={"owner_domain_id": OwnerDomainId("owner-somebody-else")}
            ),
            reason="test",
            command_id="revoke-1",
            correlation_id="intent-1",
            principal_id=PRINCIPAL,
        )

    # Still revocable by the owner who has it, and by an operator who is
    # withdrawing without naming one.
    result = await revoke.execute(
        device_ref=devices.device.device_ref,
        reason="test",
        command_id="revoke-1",
        correlation_id="intent-1",
        principal_id=PRINCIPAL,
    )
    assert result.outcome == "committed"
    assert devices.device.lifecycle_state is DeviceLifecycleState.REVOKED


@pytest.mark.asyncio
async def test_renaming_a_device_leaves_the_lifecycle_ledger_alone() -> None:
    """Naming something is not a lifecycle event.

    The management idempotency slot exists so a repeated approval or
    revocation is recognised as the same act rather than performed twice.
    Writing a rename into it would make the next approval look like a replay
    of something else — which is precisely the failure a Controller hit as a
    permanent 409 it could never clear.
    """

    devices = _Devices()
    devices.device = _device(
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
        last_management_request_id="approval-1",
        last_management_fingerprint="fingerprint-of-that-approval",
    )
    rename = RenameDevice(devices=devices, mutations=devices, clock=_Clock())

    renamed = await rename.execute(
        device_id="device-1",
        owner_scope="owner-1",
        display_name="  客厅的 Box-3  ",
        principal_id=PRINCIPAL,
    )

    assert renamed.display_name == "客厅的 Box-3"
    # The approval that came before is still the last management act on record.
    assert renamed.last_management_request_id == "approval-1"
    assert renamed.last_management_fingerprint == "fingerprint-of-that-approval"


@pytest.mark.asyncio
async def test_renaming_answers_to_the_owner_and_refuses_the_impossible() -> None:
    devices = _Devices()
    devices.device = _device(
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
        display_name="esp-box-3",
    )
    rename = RenameDevice(devices=devices, mutations=devices, clock=_Clock())

    with pytest.raises(PermissionError):
        await rename.execute(
            device_id="device-1",
            owner_scope="owner-somebody-else",
            display_name="客厅的 Box-3",
            principal_id=PRINCIPAL,
        )
    with pytest.raises(ValueError):
        # Whitespace would erase the name they have.
        await rename.execute(
            device_id="device-1",
            owner_scope="owner-1",
            display_name="   ",
            principal_id=PRINCIPAL,
        )

    # Setting a name to what it already is changes nothing and needs no ledger
    # to say so.
    unchanged = await rename.execute(
        device_id="device-1",
        owner_scope="owner-1",
        display_name="esp-box-3",
        principal_id=PRINCIPAL,
    )
    assert unchanged.display_name == "esp-box-3"
