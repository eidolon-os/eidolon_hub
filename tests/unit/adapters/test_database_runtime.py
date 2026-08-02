from __future__ import annotations

import pytest
from sqlalchemy import text

from hub.adapters.persistence.database import HubDatabase
from hub.composition.resources import create_database
from hub.config import (
    DeploymentConfig,
    DeviceAccessConfig,
    DiscoveryConfig,
    HubConfig,
    MdnsDiscoveryConfig,
    PostgresqlPersistenceConfig,
)


async def test_postgresql_factory_selects_asyncpg_without_leaking_into_ports() -> None:
    database = HubDatabase.postgresql("postgresql://user:secret@db.example/eidolon_hub")
    try:
        assert database.engine.url.drivername == "postgresql+asyncpg"
        assert database.engine.url.render_as_string(hide_password=True) == (
            "postgresql+asyncpg://user:***@db.example/eidolon_hub"
        )
    finally:
        await database.close()


def test_postgresql_factory_rejects_an_unrelated_driver() -> None:
    with pytest.raises(ValueError, match="postgresql scheme"):
        HubDatabase.postgresql("sqlite:///hub.sqlite3")


def _cloud_config() -> HubConfig:
    return HubConfig(
        deployment=DeploymentConfig(mode="cloud"),
        discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
        device_access=DeviceAccessConfig(public_base_url="https://hub.example.com"),
        persistence=PostgresqlPersistenceConfig(
            dsn="postgresql://db.internal:5432/eidolon_hub",
            pool_size=3,
            max_overflow=4,
            pool_timeout_seconds=7,
            pool_recycle_seconds=900,
        ),
    )


async def test_postgresql_target_and_environment_credentials_are_safely_combined(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EIDOLON_HUB_POSTGRES_USER", "hub user")
    monkeypatch.setenv("EIDOLON_HUB_POSTGRES_PASSWORD", "p@ss:/word")

    database = create_database(_cloud_config())
    try:
        assert database.engine.url.host == "db.internal"
        assert database.engine.url.database == "eidolon_hub"
        assert database.engine.url.username == "hub user"
        assert database.engine.url.password == "p@ss:/word"
        assert database.engine.pool._timeout == 7
        assert database.engine.pool._recycle == 900
    finally:
        await database.close()


def test_postgresql_credentials_are_required_separately(monkeypatch) -> None:
    monkeypatch.delenv("EIDOLON_HUB_POSTGRES_USER", raising=False)
    monkeypatch.delenv("EIDOLON_HUB_POSTGRES_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="POSTGRES_USER"):
        create_database(_cloud_config())


async def test_sqlite_migration_is_versioned_and_idempotent(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    try:
        await database.migrate()
        await database.migrate()
        await database.assert_schema_current()
        await database.assert_no_migration_drift()
        async with database.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0001"
    finally:
        await database.close()


async def test_unmigrated_database_is_rejected(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    try:
        with pytest.raises(RuntimeError, match="schema is not current"):
            await database.assert_schema_current()
    finally:
        await database.close()
