"""Hub-owned asynchronous database runtime.

SQLAlchemy is deliberately confined to the persistence adapter.  Application
and domain code only receive the small repository ports.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hub.adapters.persistence.models import Base


class HubDatabase:
    """Owns the engine, schema lifecycle and transaction factory."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.sessions = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @classmethod
    def sqlite(cls, path: str | Path, *, echo: bool = False) -> HubDatabase:
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{resolved}", echo=echo)

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return cls(engine)

    async def initialize_schema(self) -> None:
        """Create the one current schema or reject an incompatible database."""

        async with self.engine.begin() as connection:
            await connection.run_sync(self._initialize_schema)

    @staticmethod
    def _initialize_schema(connection) -> None:
        schema = inspect(connection)
        actual_tables = set(schema.get_table_names())
        expected_tables = set(Base.metadata.tables)

        if not actual_tables:
            Base.metadata.create_all(connection)
            return

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
                "remove the dedicated database and restart. " + "; ".join(problems)
            )

    async def close(self) -> None:
        await self.engine.dispose()
