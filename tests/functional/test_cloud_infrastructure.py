from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import DeviceAuthorityRow, DeviceRow
from hub.adapters.persistence.repositories import AuthorityLeaseHeld, SqlHubRepositories
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

POSTGRES_DSN = os.environ.get("EIDOLON_HUB_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="requires dedicated EIDOLON_HUB_TEST_POSTGRES_DSN",
)
async def test_real_postgresql_adapter_round_trip() -> None:
    database = HubDatabase.postgresql(POSTGRES_DSN)
    await database.init_schema()
    device_id = f"cloud-functional-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    device = ManagedDevice(
        identity=DeviceIdentity(device_id, "p256:cloud-functional", "cloud-test"),
        display_name="Cloud Functional Device",
        device_kind="reference-device",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Cloud Functional Device"}
        ),
        registered_at=now,
        updated_at=now,
    )
    try:
        repository = SqlHubRepositories(database).devices
        await repository.upsert(device)
        assert await repository.get(device_id) == device
    finally:
        async with database.sessions.begin() as session:
            await session.execute(delete(DeviceRow).where(DeviceRow.device_id == device_id))
        await database.close()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="requires dedicated EIDOLON_HUB_TEST_POSTGRES_DSN",
)
async def test_real_postgresql_fences_concurrent_hub_authority() -> None:
    first_database = HubDatabase.postgresql(POSTGRES_DSN)
    second_database = HubDatabase.postgresql(POSTGRES_DSN)
    await first_database.init_schema()
    device_id = f"cloud-authority-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    try:
        results = await asyncio.gather(
            SqlHubRepositories(first_database).authority.acquire(
                device_id=device_id,
                hub_instance_id="cloud-hub-1",
                now=now,
                ttl=timedelta(seconds=30),
            ),
            SqlHubRepositories(second_database).authority.acquire(
                device_id=device_id,
                hub_instance_id="cloud-hub-2",
                now=now,
                ttl=timedelta(seconds=30),
            ),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, AuthorityLeaseHeld) for result in results) == 1
    finally:
        async with first_database.sessions.begin() as session:
            await session.execute(
                delete(DeviceAuthorityRow).where(DeviceAuthorityRow.device_id == device_id)
            )
        await second_database.close()
        await first_database.close()
