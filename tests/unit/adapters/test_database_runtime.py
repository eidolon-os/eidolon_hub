from __future__ import annotations

import json
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
from hub.adapters.persistence.models import AuthorityStateRow, Base
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
                "admission_base_identities_v1",
                "admission_claim_grants_v1",
                "admission_claim_event_stream_v1",
                "admission_claims_v1",
                "admission_command_results_v1",
                "admission_commissioning_vouchers_v1",
                "admission_decisions_v1",
                "admission_grant_acks_v1",
                "admission_outbox_v1",
                "admission_proposals_v1",
                "hub_channel_revocation_delivery_v1",
                "hub_device_erase_ack_evidence",
                "hub_device_erase_operations",
                "hub_device_directory_v1",
                "hub_events",
                "hub_authority_state",
            }
        assert "hub_channel_assignments" not in actual
        assert "alembic_version" not in actual
    finally:
        await database.close()


async def test_ph2_admission_physical_migration_is_additive_and_preserves_authority(
    tmp_path,
) -> None:
    bootstrap_path = tmp_path / "authority-bootstrap.json"
    bootstrap_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "operation": "owner-authority.bootstrap",
                "owner_domain_id": "owner-domain_01",
                "owner_domain_generation": 3,
                "state_id": "authority-state_existing",
            }
        ),
        encoding="utf-8",
    )
    database = HubDatabase.sqlite(
        tmp_path / "hub.sqlite3",
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        authority_bootstrap_path=bootstrap_path,
    )
    admission_tables = {name for name in Base.metadata.tables if name.startswith("admission_")}
    current_grants = Base.metadata.tables["admission_claim_grants_v1"]
    legacy_metadata = MetaData()
    legacy_grants = Table(
        current_grants.name,
        legacy_metadata,
        *(
            column._copy()  # noqa: SLF001 - reproduce the frozen pre-B0 physical schema
            for column in current_grants.columns
            if column.name != "wire_envelope_json"
        ),
    )
    try:
        async with database.engine.begin() as connection:
            for table in Base.metadata.sorted_tables:
                if table.name not in {
                    "admission_claim_grants_v1",
                    "admission_claim_event_stream_v1",
                }:
                    await connection.run_sync(table.create)
            await connection.run_sync(legacy_grants.create)
            await connection.execute(
                AuthorityStateRow.__table__.insert().values(
                    singleton_id=1,
                    owner_domain_id="owner-domain_01",
                    owner_domain_generation=3,
                    state_id="authority-state_existing",
                )
            )

        await database.initialize_schema()

        async with database.engine.connect() as connection:
            actual = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        async with database.sessions() as session:
            after = await session.get(AuthorityStateRow, 1)
            after_marker = (
                after.owner_domain_id,
                after.owner_domain_generation,
                after.state_id,
            )
        assert admission_tables <= actual
        async with database.engine.connect() as connection:
            grant_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("admission_claim_grants_v1")
                }
            )
        assert "wire_envelope_json" in grant_columns
        assert "sealed_grant" not in grant_columns
        assert after_marker == ("owner-domain_01", 3, "authority-state_existing")
        assert not bootstrap_path.exists()
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


async def test_pre_claim_database_requires_explicit_restore_or_reset(tmp_path) -> None:
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

        with pytest.raises(RuntimeError, match="explicit RestoreAuthority or ResetAuthority"):
            await database.initialize_schema()

        async with database.engine.connect() as connection:
            row = (
                await connection.execute(text("SELECT device_id, owner_id FROM hub_devices"))
            ).one()
        assert row == ("device-1", "owner-1")
    finally:
        await database.close()


async def test_empty_database_cannot_rebootstrap_over_existing_authority_anchor(tmp_path) -> None:
    path = tmp_path / "hub.sqlite3"
    first = HubDatabase.sqlite(path)
    await first.initialize_schema()
    await first.close()
    path.unlink()

    replacement = HubDatabase.sqlite(path)
    try:
        with pytest.raises(RuntimeError, match="database is empty.*lineage exists"):
            await replacement.initialize_schema()
    finally:
        await replacement.close()


async def test_existing_database_requires_its_external_authority_anchor(tmp_path) -> None:
    path = tmp_path / "hub.sqlite3"
    first = HubDatabase.sqlite(path)
    await first.initialize_schema()
    await first.close()
    path.with_name(path.name + ".authority-lineage.json").unlink()

    restarted = HubDatabase.sqlite(path)
    try:
        with pytest.raises(RuntimeError, match="lineage anchor is missing"):
            await restarted.initialize_schema()
    finally:
        await restarted.close()


async def test_database_cannot_start_under_a_different_owner_generation(tmp_path) -> None:
    path = tmp_path / "hub.sqlite3"
    first = HubDatabase.sqlite(path, owner_domain_generation=1)
    await first.initialize_schema()
    await first.close()

    restarted = HubDatabase.sqlite(path, owner_domain_generation=2)
    try:
        with pytest.raises(RuntimeError, match="do not identify the same generation"):
            await restarted.initialize_schema()
    finally:
        await restarted.close()


async def test_product_bootstrap_is_explicit_one_shot_and_consumed(tmp_path) -> None:
    path = tmp_path / "hub.sqlite3"
    bootstrap = tmp_path / "authority-bootstrap.json"
    bootstrap.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "operation": "owner-authority.bootstrap",
                "owner_domain_id": "owner-test",
                "owner_domain_generation": 1,
                "state_id": "authority-state_explicit-test",
            }
        ),
        encoding="utf-8",
    )
    database = HubDatabase.sqlite(path, authority_bootstrap_path=bootstrap)
    try:
        await database.initialize_schema()
        assert not bootstrap.exists()
    finally:
        await database.close()

    restarted = HubDatabase.sqlite(path, authority_bootstrap_path=bootstrap)
    try:
        await restarted.initialize_schema()
    finally:
        await restarted.close()


async def test_product_empty_database_without_bootstrap_fails_closed(tmp_path) -> None:
    database = HubDatabase.sqlite(
        tmp_path / "hub.sqlite3",
        authority_bootstrap_path=tmp_path / "missing-bootstrap.json",
    )
    try:
        with pytest.raises(RuntimeError, match="no matching.*bootstrap capability"):
            await database.initialize_schema()
    finally:
        await database.close()


async def test_product_consumed_bootstrap_marker_is_not_a_replayable_capability(
    tmp_path,
) -> None:
    """Ops re-ships this file on every release cutover, spent.

    ``authority-bootstrap.json`` is a refreshable Host layer input, so after
    the capability has been used the controller keeps shipping the same path
    carrying ``owner-authority.bootstrap-consumed``.  A Host that later lost
    its database must not be able to bootstrap a second, empty Authority from
    that spent marker: only an explicit ResetAuthority mints a generation.
    """

    bootstrap = tmp_path / "authority-bootstrap.json"
    bootstrap.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "operation": "owner-authority.bootstrap-consumed",
                "owner_domain_id": "owner-test",
                "owner_domain_generation": 1,
                "state_id": "authority-state_already-spent",
            }
        ),
        encoding="utf-8",
    )
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3", authority_bootstrap_path=bootstrap)
    try:
        with pytest.raises(RuntimeError, match="no matching.*bootstrap capability"):
            await database.initialize_schema()
    finally:
        await database.close()
    assert bootstrap.exists()


async def test_product_consumed_bootstrap_marker_survives_an_established_hub(
    tmp_path,
) -> None:
    """Hub only ever unlinks a capability it actually used.

    A spent marker re-shipped alongside a populated database is inert, and
    leaving it in place is what lets a release cutover ship the file at all.
    """

    path = tmp_path / "hub.sqlite3"
    bootstrap = tmp_path / "authority-bootstrap.json"
    live = {
        "contract_version": 1,
        "operation": "owner-authority.bootstrap",
        "owner_domain_id": "owner-test",
        "owner_domain_generation": 1,
        "state_id": "authority-state_established",
    }
    bootstrap.write_text(json.dumps(live), encoding="utf-8")
    database = HubDatabase.sqlite(path, authority_bootstrap_path=bootstrap)
    try:
        await database.initialize_schema()
    finally:
        await database.close()
    assert not bootstrap.exists()

    spent = {**live, "operation": "owner-authority.bootstrap-consumed"}
    bootstrap.write_text(json.dumps(spent), encoding="utf-8")
    restarted = HubDatabase.sqlite(path, authority_bootstrap_path=bootstrap)
    try:
        await restarted.initialize_schema()
    finally:
        await restarted.close()
    assert json.loads(bootstrap.read_text(encoding="utf-8")) == spent


def test_the_directory_column_is_still_named_device_kind() -> None:
    """The attribute was renamed; the column deliberately was not.

    `device_kind` never held a kind — the Authority copies the Manifest id into
    it — so every reader in this process now goes through `manifest_id`. The
    physical column keeps the old name on purpose: this schema migrates at boot
    and has only ever done so additively, and a rename is not additive. An
    Authority that cannot open its own database admits nothing and answers
    nothing, which is too high a price for a name no behaviour depends on.

    So the mapping is the load-bearing part, and this is what stops someone
    "finishing" the rename by changing the column and finding out on a Pi5.
    """

    from hub.adapters.persistence.models import DeviceRow

    column = DeviceRow.__table__.c["device_kind"]
    assert DeviceRow.manifest_id.property.columns[0] is column
    assert "manifest_id" not in DeviceRow.__table__.c
