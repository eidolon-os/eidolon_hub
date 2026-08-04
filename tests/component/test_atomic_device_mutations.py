from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import DeviceManagementEventRecord
from hub.ports.repositories import ConcurrentDeviceMutationError

NOW = datetime(2026, 8, 4, tzinfo=UTC)


@pytest.fixture
async def repositories(tmp_path):
    database = HubDatabase.sqlite(tmp_path / "atomic-mutations.sqlite3")
    await database.initialize_schema()
    try:
        yield SqlHubRepositories(database)
    finally:
        await database.close()


def _device(*, display_name: str = "Device") -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity("device-1"),
        enrollment_id="enrollment-1",
        retrieval_token_hash="a" * 64,
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name=display_name,
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": display_name}
        ),
        enrolled_at=NOW,
        updated_at=NOW,
    )


def _event(*, data=None) -> DeviceManagementEventRecord:
    return DeviceManagementEventRecord(
        event_id="request-1",
        event_type="eidolon.device.enrolled.v1",
        source="eidolon-hub/device-management",
        principal_id="untrusted-device:device-1",
        subject="device-1",
        occurred_at=NOW,
        data=data or {"manifest_revision": "sha256:revision"},
    )


async def test_invalid_audit_event_rolls_back_device_fact(repositories) -> None:
    with pytest.raises(ValueError, match="256KiB"):
        await repositories.device_mutations.commit(
            expected=None,
            device=_device(),
            event=_event(data={"payload": "x" * (257 * 1024)}),
        )

    assert await repositories.devices.get("device-1") is None
    assert (
        await repositories.management_events.list_after(
            owner_scope="unclaimed", stream_position=0, limit=100
        )
        == ()
    )


async def test_stale_expected_device_cannot_overwrite_committed_fact(repositories) -> None:
    first = _device()
    await repositories.device_mutations.commit(
        expected=None,
        device=first,
        event=_event(),
    )

    with pytest.raises(ConcurrentDeviceMutationError):
        await repositories.device_mutations.commit(
            expected=None,
            device=replace(first, display_name="Concurrent overwrite"),
            event=replace(_event(), event_id="request-2"),
        )

    assert await repositories.devices.get("device-1") == first


async def test_reused_event_id_rolls_back_device_change(repositories) -> None:
    first = _device()
    await repositories.device_mutations.commit(
        expected=None,
        device=first,
        event=_event(),
    )

    changed = replace(first, display_name="Changed", updated_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="event_id"):
        await repositories.device_mutations.commit(
            expected=first,
            device=changed,
            event=replace(_event(), data={"manifest_revision": "different"}),
        )

    assert await repositories.devices.get("device-1") == first
