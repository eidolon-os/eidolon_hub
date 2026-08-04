from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from hub.adapters.persistence.database import HubDatabase
from hub.composition.resources import create_database, resolve_database_path
from hub.config import HubConfig, PersistenceConfig


async def test_local_database_factory_selects_sqlite_and_resolves_path(tmp_path) -> None:
    config = HubConfig(persistence=PersistenceConfig(path=str(tmp_path / "hub.sqlite3")))
    database = create_database(config)
    try:
        assert database.engine.url.drivername == "sqlite+aiosqlite"
        assert resolve_database_path(config) == (tmp_path / "hub.sqlite3").resolve()
    finally:
        await database.close()


async def test_sqlite_schema_is_created_from_current_orm_and_is_idempotent(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    try:
        await database.initialize_schema()
        await database.initialize_schema()

        def table_names(connection) -> set[str]:
            return set(inspect(connection).get_table_names())

        async with database.engine.connect() as connection:
            actual = await connection.run_sync(table_names)

        assert actual == {
            "hub_devices",
            "hub_events",
        }
        assert "hub_channel_assignments" not in actual
        assert "alembic_version" not in actual
    finally:
        await database.close()


async def test_legacy_schema_is_rejected_instead_of_upgraded(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "legacy.sqlite3")
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE hub_devices ("
                    "device_id VARCHAR(255) PRIMARY KEY, "
                    "tenant_id VARCHAR(255) NOT NULL, "
                    "approved BOOLEAN NOT NULL, "
                    "revoked BOOLEAN NOT NULL)"
                )
            )

        with pytest.raises(RuntimeError, match="does not match the current ORM schema"):
            await database.initialize_schema()

        async with database.engine.connect() as connection:
            columns = {
                row[1] for row in await connection.execute(text("PRAGMA table_info(hub_devices)"))
            }
        assert columns == {"device_id", "tenant_id", "approved", "revoked"}
    finally:
        await database.close()
