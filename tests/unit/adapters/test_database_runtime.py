from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    text,
)

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
                "hub_claim_command_results",
                "hub_claim_events",
                "hub_device_control_operations",
                "hub_device_erase_ack_evidence",
                "hub_device_erase_operations",
                "hub_device_operation_key_bindings",
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


async def test_pre_claim_database_is_cut_over_once_and_preserves_device_rows(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "pre-claim.sqlite3")
    metadata = MetaData()
    devices = Table(
        "hub_devices",
        metadata,
        Column("device_id", String(255), primary_key=True),
        Column("enrollment_id", String(255), nullable=False),
        Column("retrieval_token_hash", String(64), nullable=False),
        Column("retrieval_expires_at", DateTime(timezone=True), nullable=False, index=True),
        Column("display_name", String(512), nullable=False),
        Column("device_kind", String(255), nullable=False, index=True),
        Column("manifest_json", Text, nullable=False),
        Column("manifest_revision", String(80), nullable=False),
        Column("enrolled_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False, index=True),
        Column("last_enrollment_request_id", String(255), nullable=False),
        Column("last_enrollment_fingerprint", String(64), nullable=False),
        Column("owner_id", String(255), nullable=True, index=True),
        Column("lifecycle_state", String(32), nullable=False, index=True),
        Column("last_management_request_id", String(255), nullable=False),
        Column("last_management_fingerprint", String(128), nullable=False),
        Index("ix_hub_devices_enrollment", "enrollment_id", unique=True),
    )
    Table(
        "hub_events",
        metadata,
        Column("stream_position", Integer, primary_key=True, autoincrement=True),
        Column("event_id", String(255), nullable=False, unique=True, index=True),
        Column("event_type", String(255), nullable=False, index=True),
        Column("source", String(512), nullable=False),
        Column("principal_id", String(255), nullable=False, index=True),
        Column("subject", String(512), nullable=False, index=True),
        Column("owner_id", String(255), nullable=False, index=True),
        Column("occurred_at", DateTime(timezone=True), nullable=False, index=True),
        Column("data_json", Text, nullable=False),
    )
    now = datetime(2026, 8, 23, tzinfo=UTC)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                devices.insert().values(
                    device_id="device-1",
                    enrollment_id="enrollment-1",
                    retrieval_token_hash="a" * 64,
                    retrieval_expires_at=now,
                    display_name="Device",
                    device_kind="generic",
                    manifest_json='{"schema_version":1}',
                    manifest_revision=(
                        "sha256:eb32877fcea1486b23bd7f5347cf7e246aee15bb3dc9c2f7ff88ce1f9439d7de"
                    ),
                    enrolled_at=now,
                    updated_at=now,
                    last_enrollment_request_id="enrollment-request-1",
                    last_enrollment_fingerprint="b" * 64,
                    owner_id="owner-1",
                    lifecycle_state="approved",
                    last_management_request_id="",
                    last_management_fingerprint="",
                )
            )

        await database.initialize_schema()
        await database.initialize_schema()

        async with database.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT claim_generation, trust_epoch, aggregate_revision "
                        "FROM hub_devices WHERE device_id = 'device-1'"
                    )
                )
            ).one()
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert row == (1, 1, 1)
        assert "hub_claim_command_results" in tables
        assert "hub_claim_events" in tables
        assert "hub_device_erase_operations" in tables
    finally:
        await database.close()
