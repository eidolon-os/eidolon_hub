from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.presentation import OutputSelection
from eidolon_sdk.device_foundation.v1 import BusinessOwnerId, ControllerActorRef, OwnerDomainId
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.admission.domain import ActorContext, AdmissionProblem
from hub.device_control.output_policy import (
    POLICY_READ_SCOPE,
    POLICY_WRITE_SCOPE,
    OutputPolicyConflict,
    ReadDeviceOutputConfiguration,
    ReadOutputPolicy,
    SetOutputPolicy,
    UpdateDeviceOutputPolicy,
)
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import DeviceManagementEventRecord

NOW = datetime(2026, 9, 15, tzinfo=UTC)


#: What a companion-face device declares about itself: it can receive audio,
#: it can show this product's face, and it can show dialogue text.
COMPANION_MANIFEST = {
    "schema_version": 1,
    "title": "Companion",
    "media": [{"kind": "audio", "direction": "bidirectional", "codecs": ["opus"]}],
    "properties": [
        {
            "name": "expression.profile",
            "observable": False,
            "writable": False,
            "schema": {"type": "string", "const": "eidolon.face.v1"},
        },
        {
            "name": "output.dialogue_text",
            "observable": False,
            "writable": False,
            "schema": {"type": "boolean", "const": True},
        },
    ],
}


def actor(owner="owner_1", scopes=(POLICY_WRITE_SCOPE, POLICY_READ_SCOPE)):
    return ActorContext(
        actor=ControllerActorRef(
            principal_id="controller-1",
            principal_type="controller",
            owner_domain_id="owner-test",
            granted_scopes=scopes,
            authentication_strength="software",
        ),
        owner_domain_id=OwnerDomainId("owner-test"),
        business_owner_id=BusinessOwnerId(owner),
    )


@pytest.fixture
async def subject(tmp_path):
    db = HubDatabase.sqlite(tmp_path / "policy.sqlite3")
    await db.initialize_schema()
    repos = SqlHubRepositories(db)
    device = ManagedDevice(
        identity=DeviceIdentity(named_device_instance_id("policy-device")),
        display_name="Companion",
        manifest_id="companion",
        manifest=DeviceManifestDocument.from_declaration(
            document=COMPANION_MANIFEST, declared_revision=1
        ),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_id="owner_1",
    )
    await repos.device_mutations.commit(
        expected=None,
        device=device,
        event=DeviceManagementEventRecord(
            event_id="seed",
            event_type="device.created",
            source="test",
            principal_id="test",
            subject=device.identity.device_id,
            occurred_at=NOW,
            data={"owner_id": "owner_1"},
        ),
    )
    counter = iter(range(100))
    service = UpdateDeviceOutputPolicy(
        devices=repos.devices,
        mutations=repos.device_mutations,
        clock=SimpleNamespace(now=lambda: NOW),
        ids=SimpleNamespace(new=lambda prefix: f"{prefix}-{next(counter)}"),
    )
    yield service, repos, device, db
    await db.close()


async def test_policy_persists_with_revision_and_audit_and_idempotent_retry(subject):
    service, repos, device, db = subject
    cmd = SetOutputPolicy(
        device_ref=device.device_ref, expected_revision=0, allowed=OutputSelection(expression=True)
    )
    policy = await service.execute(command=cmd, context=actor())
    assert policy.revision == 1 and not policy.allowed.speech
    assert await service.execute(command=cmd, context=actor()) == policy
    stored = await repos.devices.get(device.identity.device_id)
    assert stored.output_policy == policy and stored.aggregate_revision == 2
    # Re-opening the schema must preserve the policy and Authority lineage.
    await db.initialize_schema()
    assert (await repos.devices.get(device.identity.device_id)).output_policy == policy
    events = await repos.management_events.list_after(
        owner_scope="owner_1", stream_position=0, limit=10
    )
    changes = [e for e in events if e.event.event_type == "device.output-policy.changed"]
    assert len(changes) == 1


async def test_foreign_owner_stale_revision_and_missing_scope_cannot_change_policy(subject):
    service, repos, device, _ = subject
    cmd = SetOutputPolicy(
        device_ref=device.device_ref, expected_revision=0, allowed=OutputSelection(expression=True)
    )
    with pytest.raises(PermissionError):
        await service.execute(command=cmd, context=actor("owner_other"))
    with pytest.raises(AdmissionProblem):
        await service.execute(command=cmd, context=actor(scopes=("admission.read",)))
    await service.execute(command=cmd, context=actor())
    voice = cmd.model_copy(update={"allowed": OutputSelection(speech=True)})
    with pytest.raises(OutputPolicyConflict):
        await service.execute(command=voice, context=actor())
    assert not (await repos.devices.get(device.identity.device_id)).output_policy.allowed.speech


async def test_read_answers_declared_capability_and_the_undecided_policy(subject):
    """Capability comes from the Manifest; ``None`` means nobody has decided."""

    service, repos, device, _ = subject
    reader = ReadDeviceOutputConfiguration(devices=repos.devices)
    query = ReadOutputPolicy(device_ref=device.device_ref)

    before = await reader.execute(query=query, context=actor())
    assert before.capabilities == OutputSelection(speech=True, dialogue_text=True, expression=True)
    assert before.policy is None
    assert before.policy_required is True

    await service.execute(
        command=SetOutputPolicy(
            device_ref=device.device_ref,
            expected_revision=0,
            allowed=OutputSelection(expression=True),
        ),
        context=actor(),
    )
    after = await reader.execute(query=query, context=actor())
    # The Owner narrowing what may be used never narrows what the device is.
    assert after.capabilities == before.capabilities
    assert after.policy is not None and after.policy.allowed == OutputSelection(expression=True)


async def test_read_refuses_exactly_what_the_write_refuses(subject):
    _, repos, device, _ = subject
    reader = ReadDeviceOutputConfiguration(devices=repos.devices)
    query = ReadOutputPolicy(device_ref=device.device_ref)

    with pytest.raises(PermissionError):
        await reader.execute(query=query, context=actor("owner_other"))
    with pytest.raises(AdmissionProblem):
        await reader.execute(query=query, context=actor(scopes=(POLICY_WRITE_SCOPE,)))
    older = device.device_ref.model_copy(update={"claim_generation": 2})
    with pytest.raises(OutputPolicyConflict):
        await reader.execute(query=ReadOutputPolicy(device_ref=older), context=actor())


async def test_read_projects_explicit_non_face_contract(subject):
    from dataclasses import replace

    _, _, device, _ = subject
    manifest = dict(COMPANION_MANIFEST)
    manifest["properties"] = [
        p for p in manifest["properties"] if p["name"] != "expression.profile"
    ]
    manifest["properties"].append(
        {
            "name": "output.contract",
            "writable": False,
            "schema": {"type": "string", "const": "eidolon.outputs.v1"},
        }
    )
    changed = replace(
        device,
        manifest=DeviceManifestDocument.from_declaration(document=manifest, declared_revision=2),
    )

    class Repository:
        async def get(self, device_id):
            return changed

    result = await ReadDeviceOutputConfiguration(devices=Repository()).execute(
        query=ReadOutputPolicy(device_ref=changed.device_ref), context=actor()
    )
    assert not result.capabilities.expression
    assert result.policy_required is True
    assert result.policy is None
