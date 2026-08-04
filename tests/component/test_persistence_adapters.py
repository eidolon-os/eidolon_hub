from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

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


def _device(device_id="device-1", enrollment_id="enrollment-1"):
    return ManagedDevice(
        identity=DeviceIdentity(device_id),
        enrollment_id=enrollment_id,
        retrieval_token_hash="a" * 64,
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name="Generic Sensor",
        device_kind="environment-sensor",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Generic Sensor"}
        ),
        enrolled_at=NOW,
        updated_at=NOW,
    )


async def test_device_round_trip_and_enrollment_lookup(database) -> None:
    repository = SqlHubRepositories(database).devices
    device = _device()
    await repository.upsert(device)
    await repository.upsert(device)

    assert await repository.get("device-1") == device
    assert await repository.get_by_enrollment_id("enrollment-1") == device
    assert await repository.get_by_enrollment_id("missing") is None
    assert await repository.list_all() == (device,)


async def test_enrollment_id_is_unique_across_devices(database) -> None:
    repository = SqlHubRepositories(database).devices
    await repository.upsert(_device())

    with pytest.raises(IntegrityError):
        await repository.upsert(_device("device-2", "enrollment-1"))


async def test_management_event_stream_is_owner_scoped_and_idempotent(database) -> None:
    repositories = SqlHubRepositories(database)
    approved = replace(
        _device(), owner_id="owner-1", lifecycle_state=DeviceLifecycleState.APPROVED
    )
    await repositories.devices.upsert(approved)
    event = DeviceManagementEventRecord(
        event_id="event-1",
        event_type="eidolon.device.approved.v1",
        source="eidolon-hub/device-management",
        subject="device-1",
        occurred_at=NOW,
        data={"owner_id": "owner-1"},
    )
    await repositories.management_events.publish(event)
    await repositories.management_events.publish(event)

    stream = await repositories.management_events.list_after(
        owner_scope="owner-1", stream_position=0, limit=100
    )
    assert [item.event for item in stream] == [event]
    assert await repositories.management_events.list_after(
        owner_scope="owner-2", stream_position=0, limit=100
    ) == ()


async def test_management_event_id_cannot_be_reused_with_other_content(database) -> None:
    repositories = SqlHubRepositories(database)
    await repositories.devices.upsert(_device())
    event = DeviceManagementEventRecord(
        event_id="event-1",
        event_type="eidolon.device.enrolled.v1",
        source="eidolon-hub/device-management",
        subject="device-1",
        occurred_at=NOW,
        data={"ok": True},
    )
    await repositories.management_events.publish(event)
    with pytest.raises(ValueError, match="reused"):
        await repositories.management_events.publish(replace(event, data={"ok": False}))
