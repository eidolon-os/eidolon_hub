from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import DeviceManagementEventRecord

NOW = datetime(2026, 8, 4, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path):
    value = HubDatabase.sqlite(tmp_path / "directory.sqlite3")
    await value.initialize_schema()
    try:
        yield value
    finally:
        await value.close()


def _device():
    return ManagedDevice(
        identity=DeviceIdentity("device-1"),
        enrollment_id="enrollment-1",
        retrieval_token_hash="secret-hash-not-public",
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_id="owner-1",
        lifecycle_state=DeviceLifecycleState.APPROVED,
    )


def _event(device, event_id):
    return DeviceManagementEventRecord(
        event_id=event_id,
        event_type="eidolon.device.approved.v1",
        source="eidolon-hub/device-management",
        principal_id="owner-operator",
        subject=device.identity.device_id,
        occurred_at=device.updated_at,
        data={"owner_id": device.owner_id},
    )


async def test_directory_projects_only_safe_device_metadata(database) -> None:
    repositories = SqlHubRepositories(database)
    device = _device()
    await repositories.device_mutations.commit(
        expected=None, device=device, event=_event(device, "event-1")
    )
    devices = repositories.devices
    directory = InMemoryDeviceDirectoryRepository()
    projector = ProjectDeviceDirectory(devices=devices, directory=directory)

    entry = await projector.execute("device-1")

    assert entry.device_id == "device-1"
    assert not hasattr(entry, "retrieval_token_hash")
    assert not hasattr(entry, "online")


async def test_directory_is_rebuilt_from_device_table_after_restart(database) -> None:
    repositories = SqlHubRepositories(database)
    device = _device()
    await repositories.device_mutations.commit(
        expected=None, device=device, event=_event(device, "event-1")
    )
    devices = repositories.devices
    restarted_directory = InMemoryDeviceDirectoryRepository()

    projected = await ProjectDeviceDirectory(
        devices=devices, directory=restarted_directory
    ).execute_all()

    assert projected[0].device_id == "device-1"
    assert await restarted_directory.list(owner_scope="owner-1") == projected


async def test_owner_scope_change_removes_old_memory_visibility(database) -> None:
    repositories = SqlHubRepositories(database)
    original = _device()
    await repositories.device_mutations.commit(
        expected=None, device=original, event=_event(original, "event-1")
    )
    devices = repositories.devices
    directory = InMemoryDeviceDirectoryRepository()
    projector = ProjectDeviceDirectory(devices=devices, directory=directory)
    await projector.execute("device-1")
    changed = replace(original, owner_id="owner-2")
    await repositories.device_mutations.commit(
        expected=original, device=changed, event=_event(changed, "event-2")
    )
    transferred = await projector.execute("device-1")

    assert await directory.list(owner_scope="owner-1") == ()
    assert await directory.list(owner_scope="owner-2") == (transferred,)
