"""Hub-owned asynchronous database runtime.

SQLAlchemy is deliberately confined to the persistence adapter.  Application
and domain code only receive the small repository ports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hub.adapters.persistence.models import AuthorityStateRow, Base


class HubDatabase:
    """Owns the engine, schema lifecycle and transaction factory."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        owner_domain_id: str,
        owner_domain_generation: int,
        authority_anchor_path: Path,
        authority_bootstrap_path: Path | None,
    ) -> None:
        self.engine = engine
        self.owner_domain_id = owner_domain_id
        self.owner_domain_generation = owner_domain_generation
        self.authority_anchor_path = authority_anchor_path
        self.authority_bootstrap_path = authority_bootstrap_path
        self.sessions = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        owner_domain_id: str = "owner-test",
        owner_domain_generation: int = 1,
        authority_anchor_path: str | Path | None = None,
        authority_bootstrap_path: str | Path | None = None,
        echo: bool = False,
    ) -> HubDatabase:
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        anchor = (
            Path(authority_anchor_path).expanduser().resolve()
            if authority_anchor_path is not None
            else resolved.with_name(resolved.name + ".authority-lineage.json")
        )
        bootstrap = (
            Path(authority_bootstrap_path).expanduser().resolve()
            if authority_bootstrap_path is not None
            else None
        )
        engine = create_async_engine(f"sqlite+aiosqlite:///{resolved}", echo=echo)

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return cls(
            engine,
            owner_domain_id=owner_domain_id,
            owner_domain_generation=owner_domain_generation,
            authority_anchor_path=anchor,
            authority_bootstrap_path=bootstrap,
        )

    async def initialize_schema(self) -> None:
        """Create first-install state or verify the existing Authority lineage."""

        anchor = self._read_anchor()
        bootstrap = self._read_bootstrap()
        async with self.engine.begin() as connection:
            create_anchor, marker, consume_bootstrap = await connection.run_sync(
                self._initialize_schema,
                self.owner_domain_id,
                self.owner_domain_generation,
                anchor,
                bootstrap,
                self.authority_bootstrap_path is None,
            )
        if create_anchor:
            self._create_anchor(marker)
        if consume_bootstrap:
            self._consume_bootstrap()

    @staticmethod
    def _initialize_schema(
        connection,
        owner_domain_id: str,
        owner_domain_generation: int,
        anchor: dict[str, object] | None,
        bootstrap: dict[str, object] | None,
        allow_implicit_test_bootstrap: bool,
    ) -> tuple[bool, dict[str, object], bool]:
        schema = inspect(connection)
        actual_tables = set(schema.get_table_names())
        expected_tables = set(Base.metadata.tables)

        if not actual_tables:
            if anchor is not None:
                raise RuntimeError(
                    "AuthorityRecoveryRequired: Hub database is empty but an established "
                    "Owner Authority lineage exists"
                )
            if bootstrap is None and allow_implicit_test_bootstrap:
                bootstrap = {
                    "contract_version": 1,
                    "operation": "owner-authority.bootstrap",
                    "owner_domain_id": owner_domain_id,
                    "owner_domain_generation": owner_domain_generation,
                    "state_id": "authority-state_test-only",
                }
            expected_bootstrap = {
                "contract_version": 1,
                "operation": "owner-authority.bootstrap",
                "owner_domain_id": owner_domain_id,
                "owner_domain_generation": owner_domain_generation,
            }
            if (
                bootstrap is None
                or any(bootstrap.get(key) != value for key, value in expected_bootstrap.items())
                or not isinstance(bootstrap.get("state_id"), str)
                or not bootstrap["state_id"].startswith("authority-state_")
            ):
                raise RuntimeError(
                    "AuthorityRecoveryRequired: empty Hub database has no matching "
                    "first-install or ResetAuthority bootstrap capability"
                )
            Base.metadata.create_all(connection)
            marker = {
                "contract_version": 1,
                "owner_domain_id": owner_domain_id,
                "owner_domain_generation": owner_domain_generation,
                "state_id": bootstrap["state_id"],
            }
            connection.execute(
                AuthorityStateRow.__table__.insert().values(
                    singleton_id=1,
                    owner_domain_id=owner_domain_id,
                    owner_domain_generation=owner_domain_generation,
                    state_id=bootstrap["state_id"],
                )
            )
            return True, marker, True

        # Admission physical migration adds only the canonical wire envelope.
        # No legacy ciphertext is read, copied or interpreted by the activated
        # writer; an existing Authority lineage remains unchanged.
        grant_table = "admission_claim_grants_v1"
        if grant_table in actual_tables:
            grant_columns = {value["name"] for value in schema.get_columns(grant_table)}
            if "wire_envelope_json" not in grant_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE admission_claim_grants_v1 ADD COLUMN wire_envelope_json TEXT"
                )
                schema = inspect(connection)

        # A device's Manifest revision is additive to the directory projection.
        # Existing rows carry the revision their Claim recorded, which is 1: it
        # is the only account of themselves those devices have ever given.
        directory_table = "hub_device_directory_v1"
        if directory_table in actual_tables:
            directory_columns = {value["name"] for value in schema.get_columns(directory_table)}
            if "manifest_declared_revision" not in directory_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE hub_device_directory_v1 "
                    "ADD COLUMN manifest_declared_revision INTEGER NOT NULL DEFAULT 1"
                )
                schema = inspect(connection)

        missing_tables = expected_tables - actual_tables
        if missing_tables and all(
            table_name.startswith("admission_")
            or table_name == "hub_channel_revocation_delivery_v1"
            for table_name in missing_tables
        ):
            for table_name in sorted(missing_tables):
                Base.metadata.tables[table_name].create(connection, checkfirst=True)
            schema = inspect(connection)
            actual_tables = set(schema.get_table_names())

        problems: list[str] = []
        if actual_tables != expected_tables:
            missing = sorted(expected_tables - actual_tables)
            unexpected = sorted(actual_tables - expected_tables)
            problems.append(f"tables missing={missing}, unexpected={unexpected}")

        for table_name in sorted(actual_tables & expected_tables):
            table = Base.metadata.tables[table_name]
            reflected_columns = schema.get_columns(table_name)
            actual_columns = {value["name"] for value in reflected_columns}
            expected_columns = set(table.columns.keys())
            if actual_columns != expected_columns:
                missing = sorted(expected_columns - actual_columns)
                unexpected = sorted(actual_columns - expected_columns)
                problems.append(f"{table_name} columns missing={missing}, unexpected={unexpected}")
                continue

            actual_contract = {
                value["name"]: (
                    value["type"].compile(dialect=connection.dialect).upper(),
                    bool(value["nullable"]),
                    bool(value["primary_key"]),
                )
                for value in reflected_columns
            }
            expected_contract = {
                column.name: (
                    column.type.compile(dialect=connection.dialect).upper(),
                    bool(column.nullable),
                    bool(column.primary_key),
                )
                for column in table.columns
            }
            if actual_contract != expected_contract:
                problems.append(f"{table_name} column definitions differ")

            actual_indexes = {
                (
                    value["name"],
                    tuple(value["column_names"]),
                    bool(value["unique"]),
                )
                for value in schema.get_indexes(table_name)
            }
            expected_indexes = {
                (
                    index.name,
                    tuple(column.name for column in index.columns),
                    bool(index.unique),
                )
                for index in table.indexes
            }
            if actual_indexes != expected_indexes:
                problems.append(f"{table_name} indexes differ")

        if problems:
            raise RuntimeError(
                "Hub database does not match the current ORM schema; "
                "use the explicit RestoreAuthority or ResetAuthority workflow. "
                + "; ".join(problems)
            )

        row = (
            connection.execute(
                AuthorityStateRow.__table__.select().where(AuthorityStateRow.singleton_id == 1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RuntimeError(
                "AuthorityRecoveryRequired: Hub database Authority marker is missing"
            )
        marker = {
            "contract_version": 1,
            "owner_domain_id": row["owner_domain_id"],
            "owner_domain_generation": row["owner_domain_generation"],
            "state_id": row["state_id"],
        }
        expected = {
            "contract_version": 1,
            "owner_domain_id": owner_domain_id,
            "owner_domain_generation": owner_domain_generation,
            "state_id": row["state_id"],
        }
        bootstrap_marker = {
            "contract_version": 1,
            "operation": "owner-authority.bootstrap",
            **expected,
        }
        if anchor is None:
            if bootstrap != bootstrap_marker:
                raise RuntimeError(
                    "AuthorityRecoveryRequired: Hub database exists but its external "
                    "Authority lineage anchor is missing"
                )
            return True, marker, True
        if marker != expected or anchor != expected:
            raise RuntimeError(
                "AuthorityRecoveryRequired: Hub database, Owner configuration, and "
                "Authority lineage anchor do not identify the same generation"
            )
        return False, marker, bootstrap == bootstrap_marker

    def _read_anchor(self) -> dict[str, object] | None:
        if not self.authority_anchor_path.exists():
            return None
        try:
            value = json.loads(self.authority_anchor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "AuthorityRecoveryRequired: Authority lineage anchor is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError("AuthorityRecoveryRequired: Authority lineage anchor is invalid")
        return value

    def _read_bootstrap(self) -> dict[str, object] | None:
        path = self.authority_bootstrap_path
        if path is None or not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "AuthorityRecoveryRequired: Authority bootstrap capability is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError(
                "AuthorityRecoveryRequired: Authority bootstrap capability is invalid"
            )
        return value

    def _consume_bootstrap(self) -> None:
        path = self.authority_bootstrap_path
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(
                "AuthorityRecoveryRequired: bootstrap capability could not be consumed"
            ) from exc

    def _create_anchor(self, marker: dict[str, object]) -> None:
        self.authority_anchor_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            descriptor = os.open(
                self.authority_anchor_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                "AuthorityRecoveryRequired: Authority lineage anchor appeared during bootstrap"
            ) from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def close(self) -> None:
        await self.engine.dispose()
