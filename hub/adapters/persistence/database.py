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
from sqlalchemy.schema import CreateColumn

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

        # Additive columns are reconciled from the ORM rather than listed here
        # by hand, because the hand-written list was itself the defect. The
        # release that introduced `output_policy_json` declared it on two
        # tables and wrote the ALTER for one of them, and nothing anywhere
        # could notice the difference: a list has nothing to be compared
        # against. Every Host holding an existing database then met a Hub that
        # refused to start on it — `admission_proposals_v1 columns
        # missing=['output_policy_json']` — with no way back, because the
        # release that could have been rolled back to had already been left.
        #
        # Only what can be added without inventing a value for rows that
        # already exist: a nullable column gets NULL, and one carrying a server
        # default gets that default. A mandatory column with neither is a
        # statement about every existing row, which is a decision this code is
        # not entitled to make on its own — those stay refused below, and the
        # one place such a decision has been made spells it out immediately
        # after this.
        for table_name in sorted(expected_tables & actual_tables):
            table = Base.metadata.tables[table_name]
            present = {value["name"] for value in schema.get_columns(table_name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.server_default is None:
                    continue
                definition = CreateColumn(column).compile(dialect=connection.dialect).string
                connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {definition}")
                schema = inspect(connection)

        # A device's Manifest revision is additive to the directory projection,
        # and unlike the columns above it is mandatory, so adding it says
        # something about rows that are already there. Existing rows carry the
        # revision their Claim recorded, which is 1: it is the only account of
        # themselves those devices have ever given.
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

        # What this compares for, and why the two directions are not the same
        # thing. A database this Hub cannot use is one that is *missing*
        # something it needs — a legacy or foreign lineage, which is what the
        # RestoreAuthority and ResetAuthority workflows exist for. A database
        # that has *more* than this Hub knows about is a different situation
        # with a different right answer: a newer release ran here and added
        # something, and then the release was rolled back.
        #
        # Treating those alike made a reversible cutover a promise the
        # deployment could not keep. On 2026-09-15 a release whose Hub added
        # one nullable column to hub_device_directory_v1 activated, migrated
        # this file, and was rolled back when an unrelated readiness check
        # timed out. The rollback restored the code, because that is a symlink,
        # and could not restore the schema, because that is not. The Host came
        # back on its previous release with a Hub that refused to start at all,
        # and the restore that was supposed to protect it had already run.
        #
        # So the rule is what this code actually depends on: everything it
        # reads must be there, and nothing it does not know about may refuse
        # its writes. A column it never names costs it nothing — unless the
        # column is mandatory and has no default, in which case every INSERT
        # this Hub makes would fail on it. A unique index it never declared is
        # the same hazard by another route: it can reject a row this Hub
        # considers valid. Those two stay fatal. Anything else additive is
        # invisible from here and is allowed to be.
        problems: list[str] = []
        missing_expected_tables = sorted(expected_tables - actual_tables)
        if missing_expected_tables:
            problems.append(f"tables missing={missing_expected_tables}")

        for table_name in sorted(actual_tables & expected_tables):
            table = Base.metadata.tables[table_name]
            reflected_columns = schema.get_columns(table_name)
            actual_columns = {value["name"] for value in reflected_columns}
            expected_columns = set(table.columns.keys())
            missing_columns = sorted(expected_columns - actual_columns)
            if missing_columns:
                problems.append(f"{table_name} columns missing={missing_columns}")
                continue

            unwritable = sorted(
                value["name"]
                for value in reflected_columns
                if value["name"] not in expected_columns
                and not value["nullable"]
                and value.get("default") is None
            )
            if unwritable:
                problems.append(
                    f"{table_name} has columns this release cannot write and cannot "
                    f"leave empty: {unwritable}"
                )

            actual_contract = {
                value["name"]: (
                    value["type"].compile(dialect=connection.dialect).upper(),
                    bool(value["nullable"]),
                    bool(value["primary_key"]),
                )
                for value in reflected_columns
                if value["name"] in expected_columns
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
            missing_indexes = sorted(
                name for name, _columns, _unique in expected_indexes - actual_indexes
            )
            if missing_indexes:
                problems.append(f"{table_name} indexes missing={missing_indexes}")
            rejecting_indexes = sorted(
                name
                for name, _columns, unique in actual_indexes - expected_indexes
                if unique
            )
            if rejecting_indexes:
                problems.append(
                    f"{table_name} has unique indexes this release did not declare and "
                    f"could be refused by: {rejecting_indexes}"
                )

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
