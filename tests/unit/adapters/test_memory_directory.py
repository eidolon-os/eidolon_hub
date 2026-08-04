from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.domain.devices.entities import DeviceDirectoryEntry, DeviceLifecycleState
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 4, tzinfo=UTC)


def _entry(device_id="device-1", owner_scope="owner-1"):
    return DeviceDirectoryEntry(
        device_id=device_id,
        owner_scope=owner_scope,
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Device"}
        ),
        lifecycle_state=DeviceLifecycleState.APPROVED,
        enrolled_at=NOW,
        updated_at=NOW,
    )


async def test_memory_directory_is_owner_scoped_and_sorted() -> None:
    directory = InMemoryDeviceDirectoryRepository()
    await directory.upsert(_entry("device-b"))
    await directory.upsert(_entry("device-a"))
    await directory.upsert(_entry("device-c", "owner-2"))

    assert await directory.get(owner_scope="owner-2", device_id="device-a") is None
    assert [
        item.device_id for item in await directory.list(owner_scope="owner-1")
    ] == ["device-a", "device-b"]


async def test_memory_directory_upsert_replaces_projection() -> None:
    directory = InMemoryDeviceDirectoryRepository()
    first = await directory.upsert(_entry())
    changed = await directory.upsert(replace(first, display_name="Changed"))

    assert changed.display_name == "Changed"
    assert (
        await directory.get(owner_scope="owner-1", device_id="device-1")
    ) == changed
