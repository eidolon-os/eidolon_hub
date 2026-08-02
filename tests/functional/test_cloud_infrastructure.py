from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.repositories import AuthorityLeaseHeld, SqlHubRepositories
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

POSTGRES_DSN = os.environ.get("EIDOLON_HUB_TEST_POSTGRES_DSN", "").strip()


@pytest.fixture
async def postgres_database_factory() -> Callable[[], HubDatabase]:
    if not POSTGRES_DSN:
        pytest.skip("requires dedicated EIDOLON_HUB_TEST_POSTGRES_DSN")
    schema = f"hub_test_{uuid.uuid4().hex}"
    administrator = HubDatabase.postgresql(POSTGRES_DSN)
    async with administrator.engine.begin() as connection:
        await connection.execute(CreateSchema(schema))

    databases: list[HubDatabase] = []

    def create() -> HubDatabase:
        database = HubDatabase.postgresql(POSTGRES_DSN, search_path=schema)
        databases.append(database)
        return database

    try:
        yield create
    finally:
        for database in reversed(databases):
            await database.close()
        async with administrator.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await administrator.close()


async def test_real_postgresql_migration_and_adapter_round_trip(
    postgres_database_factory,
) -> None:
    database = postgres_database_factory()
    await database.migrate()
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

    repository = SqlHubRepositories(database).devices
    await repository.upsert(device)

    assert await repository.get(device_id) == device


async def test_real_postgresql_fences_concurrent_hub_authority(
    postgres_database_factory,
) -> None:
    first_database = postgres_database_factory()
    second_database = postgres_database_factory()
    await first_database.migrate()
    device_id = f"cloud-authority-{uuid.uuid4().hex}"
    now = datetime.now(UTC)

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
