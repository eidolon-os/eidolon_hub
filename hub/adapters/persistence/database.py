"""Hub-owned asynchronous database runtime.

SQLAlchemy is deliberately confined to the persistence adapter.  Application
and domain code only receive the small repository ports.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_MIGRATIONS = Path(__file__).with_name("migrations")
_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    @classmethod
    def postgresql(
        cls,
        dsn: str,
        *,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout_seconds: float = 10.0,
        pool_recycle_seconds: int = 1800,
        echo: bool = False,
        search_path: str | None = None,
    ) -> HubDatabase:
        normalized = dsn.strip()
        if normalized.startswith("postgresql://"):
            normalized = "postgresql+asyncpg://" + normalized.removeprefix("postgresql://")
        if not normalized.startswith("postgresql+asyncpg://"):
            raise ValueError("PostgreSQL DSN must use the postgresql scheme")
        if search_path is not None and not _SCHEMA_NAME.fullmatch(search_path):
            raise ValueError("PostgreSQL search_path must be one schema identifier")
        connect_args = (
            {"server_settings": {"search_path": search_path}} if search_path is not None else {}
        )
        return cls(
            create_async_engine(
                normalized,
                pool_pre_ping=True,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout_seconds,
                pool_recycle=pool_recycle_seconds,
                echo=echo,
                connect_args=connect_args,
            )
        )

    async def migrate(self) -> None:
        """Upgrade the database to the packaged, versioned schema head."""

        async with self.engine.begin() as connection:
            await connection.run_sync(self._upgrade)

    async def assert_schema_current(self) -> None:
        """Fail startup when an out-of-band migration job has not reached head."""

        async with self.engine.connect() as connection:
            await connection.run_sync(self._assert_at_head)

    async def assert_no_migration_drift(self) -> None:
        """CI helper: ensure current ORM metadata needs no unversioned operation."""

        async with self.engine.connect() as connection:
            await connection.run_sync(self._check)

    @staticmethod
    def _upgrade(connection) -> None:
        config = HubDatabase._alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    @staticmethod
    def _assert_at_head(connection) -> None:
        config = HubDatabase._alembic_config()
        expected = set(ScriptDirectory.from_config(config).get_heads())
        current = set(MigrationContext.configure(connection).get_current_heads())
        if current != expected:
            raise RuntimeError(
                f"Hub database schema is not current: current={sorted(current)}, "
                f"expected={sorted(expected)}"
            )

    @staticmethod
    def _check(connection) -> None:
        config = HubDatabase._alembic_config()
        config.attributes["connection"] = connection
        command.check(config)

    @staticmethod
    def _alembic_config() -> Config:
        config = Config()
        config.set_main_option("script_location", str(_MIGRATIONS))
        return config

    async def close(self) -> None:
        await self.engine.dispose()
