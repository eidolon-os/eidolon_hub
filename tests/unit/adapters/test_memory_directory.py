from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from hub.adapters.persistence.memory import CachedDeviceDirectoryRepository
from hub.domain.devices.entities import DeviceDirectoryEntry

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _entry(*, online: bool = False) -> DeviceDirectoryEntry:
    return DeviceDirectoryEntry(
        device_id="device-1",
        owner_scope="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json="{}",
        manifest_revision="sha256:manifest",
        approved=True,
        revoked=False,
        online=online,
        connections=(),
        registered_at=NOW,
        updated_at=NOW,
    )


class _Source:
    def __init__(self) -> None:
        self.values = {"device-1": _entry()}
        self.fail_writes = False

    async def get(self, *, owner_scope, device_id):
        value = self.values.get(device_id)
        return value if value is not None and value.owner_scope == owner_scope else None

    async def upsert(self, entry):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        self.values[entry.device_id] = entry
        return entry

    async def list(self, *, owner_scope):
        return tuple(item for item in self.values.values() if item.owner_scope == owner_scope)

    async def list_all(self):
        return tuple(self.values.values())


async def test_cache_hydrates_and_updates_only_after_database_commit() -> None:
    source = _Source()
    cache = CachedDeviceDirectoryRepository(source, reconciliation_seconds=60)
    await cache.start()
    try:
        assert await cache.list(owner_scope="owner-1") == (_entry(),)
        source.fail_writes = True
        try:
            await cache.upsert(replace(_entry(), online=True))
        except RuntimeError:
            pass
        assert await cache.get(owner_scope="owner-1", device_id="device-1") == _entry()
    finally:
        await cache.stop()


async def test_refresh_reconciles_another_instance_change() -> None:
    source = _Source()
    cache = CachedDeviceDirectoryRepository(source, reconciliation_seconds=60)
    await cache.start()
    try:
        changed = replace(_entry(), online=True)
        source.values["device-1"] = changed
        await cache.refresh()
        assert await cache.get(owner_scope="owner-1", device_id="device-1") == changed
    finally:
        await cache.stop()
