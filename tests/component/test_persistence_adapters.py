from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import DeviceManagementEventRecord

NOW = datetime(2026, 8, 4, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path):
    value = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await value.initialize_schema()
    try:
        yield value
    finally:
        await value.close()


def _device(device_id="device-1"):
    return ManagedDevice(
        identity=DeviceIdentity(device_id),
        display_name="Generic Sensor",
        manifest_id="environment-sensor",
        manifest=DeviceManifestDocument.from_declaration(document=
            {"schema_version": 1, "title": "Generic Sensor"}
        , declared_revision=1),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_id="owner-1",
    )


def _enrollment_event(device, event_id):
    return DeviceManagementEventRecord(
        event_id=event_id,
        event_type="eidolon.device.enrolled.v1",
        source="eidolon-hub/device-management",
        principal_id=f"untrusted-device:{device.identity.device_id}",
        subject=device.identity.device_id,
        occurred_at=device.updated_at,
        data={"manifest_revision": device.manifest_digest},
    )


async def test_device_projection_round_trip(database) -> None:
    repositories = SqlHubRepositories(database)
    repository = repositories.devices
    device = _device()
    event = _enrollment_event(device, "event-1")
    await repositories.device_mutations.commit(
        expected=None, device=device, event=event
    )
    await repositories.device_mutations.commit(
        expected=device, device=device, event=event
    )

    assert await repository.get("device-1") == device
    assert await repository.list_all() == (device,)


async def test_management_event_stream_is_owner_scoped_and_idempotent(database) -> None:
    repositories = SqlHubRepositories(database)
    approved = replace(_device(), lifecycle_state=DeviceLifecycleState.APPROVED)
    event = DeviceManagementEventRecord(
        event_id="event-1",
        event_type="eidolon.device.approved.v1",
        source="eidolon-hub/device-management",
        principal_id="owner-operator",
        subject="device-1",
        occurred_at=NOW,
        data={"owner_id": "owner-1"},
    )
    await repositories.device_mutations.commit(
        expected=None, device=approved, event=event
    )
    await repositories.device_mutations.commit(
        expected=approved, device=approved, event=event
    )

    stream = await repositories.management_events.list_after(
        owner_scope="owner-1", stream_position=0, limit=100
    )
    assert [item.event for item in stream] == [event]
    assert await repositories.management_events.list_after(
        owner_scope="owner-2", stream_position=0, limit=100
    ) == ()


async def test_management_event_id_cannot_be_reused_with_other_content(database) -> None:
    repositories = SqlHubRepositories(database)
    device = _device()
    event = DeviceManagementEventRecord(
        event_id="event-1",
        event_type="eidolon.device.enrolled.v1",
        source="eidolon-hub/device-management",
        principal_id="untrusted-device:device-1",
        subject="device-1",
        occurred_at=NOW,
        data={"ok": True},
    )
    await repositories.device_mutations.commit(
        expected=None, device=device, event=event
    )
    with pytest.raises(ValueError, match="reused"):
        await repositories.device_mutations.commit(
            expected=device,
            device=replace(device, display_name="Changed"),
            event=replace(event, data={"ok": False}),
        )

    assert await repositories.devices.get("device-1") == device
