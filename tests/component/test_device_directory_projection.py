from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import DirectoryRow
from hub.adapters.persistence.repositories import SqlDeviceDirectoryRepository
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.domain.devices.entities import DeviceDirectoryEntry, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.domain.sessions.entities import DeviceSessionLease


@pytest.fixture
async def database(tmp_path):
    value = HubDatabase.sqlite(tmp_path / "directory.sqlite3")
    await value.init_schema()
    try:
        yield value
    finally:
        await value.close()


class _Clock:
    value = datetime(2026, 8, 1, tzinfo=UTC)

    def now(self):
        return self.value


class _Devices:
    def __init__(self, device):
        self.device = device

    async def get(self, device_id):
        return self.device if self.device.identity.device_id == device_id else None

    async def list_all(self):
        return (self.device,)


class _Sessions:
    def __init__(self, leases):
        self.leases = leases

    async def active_for_device(self, device_id, *, now):
        return tuple(
            item for item in self.leases if item.device_id == device_id and item.is_active(now)
        )


@pytest.mark.asyncio
async def test_directory_persists_metadata_without_signaling_or_channel_secrets(database) -> None:
    now = _Clock.value
    device = ManagedDevice(
        identity=DeviceIdentity("device-1", "p256:fingerprint"),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        registered_at=now,
        updated_at=now,
        owner_id="owner-1",
        approved=True,
    )
    lease = DeviceSessionLease(
        session_id="session-1",
        device_id="device-1",
        opened_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=45),
        lease_token="private-lease-token",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=1,
    )
    repository = SqlDeviceDirectoryRepository(database)
    projector = ProjectDeviceDirectory(
        devices=_Devices(device),
        sessions=_Sessions((lease,)),
        directory=repository,
        clock=_Clock(),
    )

    entry = await projector.execute("device-1")
    restored = (await SqlDeviceDirectoryRepository(database).list(owner_scope="owner-1"))[0]
    async with database.sessions() as session:
        serialized = (await session.scalar(select(DirectoryRow))).payload_json.encode()

    assert entry.online and restored == entry
    assert b"private-lease-token" not in serialized
    assert b"opaque_binding" not in serialized


@pytest.mark.asyncio
async def test_owner_transfer_atomically_removes_old_scope_visibility(database) -> None:
    now = _Clock.value
    repository = SqlDeviceDirectoryRepository(database)
    entry = DeviceDirectoryEntry(
        device_id="device-transfer",
        owner_scope="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json="{}",
        manifest_revision="sha256:manifest",
        approved=True,
        revoked=False,
        online=False,
        sessions=(),
        registered_at=now,
        updated_at=now,
    )
    await repository.upsert(entry)
    transferred = await repository.upsert(
        replace(entry, owner_scope="owner-2", updated_at=now + timedelta(seconds=1))
    )

    assert await repository.list(owner_scope="owner-1") == ()
    assert await repository.list(owner_scope="owner-2") == (transferred,)
    assert transferred.revision == 2


@pytest.mark.asyncio
async def test_reconcile_marks_naturally_expired_session_offline(database) -> None:
    now = _Clock.value
    clock = _Clock()
    device = ManagedDevice(
        identity=DeviceIdentity("device-expiry", "p256:fingerprint"),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        registered_at=now,
        updated_at=now,
        owner_id="owner-1",
        approved=True,
    )
    lease = DeviceSessionLease(
        session_id="session-expiring",
        device_id="device-expiry",
        opened_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=45),
        lease_token="lease-token",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=1,
    )
    repository = SqlDeviceDirectoryRepository(database)
    projector = ProjectDeviceDirectory(
        devices=_Devices(device),
        sessions=_Sessions((lease,)),
        directory=repository,
        clock=clock,
    )
    online = await projector.execute("device-expiry")
    clock.value = now + timedelta(seconds=46)

    projected = await projector.execute_all()

    assert online.online is True
    assert projected[0].online is False
    assert projected[0].sessions == ()
    assert projected[0].revision == 2


@pytest.mark.asyncio
async def test_unchanged_reconcile_does_not_advance_directory_revision(database) -> None:
    now = _Clock.value
    clock = _Clock()
    device = ManagedDevice(
        identity=DeviceIdentity("device-stable", "p256:fingerprint"),
        display_name="Stable Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        registered_at=now,
        updated_at=now,
        owner_id="owner-1",
        approved=True,
    )
    repository = SqlDeviceDirectoryRepository(database)
    projector = ProjectDeviceDirectory(
        devices=_Devices(device),
        sessions=_Sessions(()),
        directory=repository,
        clock=clock,
    )
    first = await projector.execute("device-stable")
    clock.value = now + timedelta(minutes=1)

    second = (await projector.execute_all())[0]

    assert second == first
    assert second.revision == 1
