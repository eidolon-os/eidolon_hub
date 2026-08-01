"""Hub-owned asynchronous database runtime.

SQLAlchemy is deliberately confined to the persistence adapter.  Application
and domain code only receive the small repository ports.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
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

    @classmethod
    def postgresql(
        cls,
        dsn: str,
        *,
        pool_size: int = 10,
        max_overflow: int = 20,
        echo: bool = False,
    ) -> HubDatabase:
        normalized = dsn.strip()
        if normalized.startswith("postgres://"):
            normalized = "postgresql://" + normalized.removeprefix("postgres://")
        if normalized.startswith("postgresql://"):
            normalized = "postgresql+asyncpg://" + normalized.removeprefix("postgresql://")
        if not normalized.startswith("postgresql+asyncpg://"):
            raise ValueError("PostgreSQL DSN must use the postgresql scheme")
        return cls(
            create_async_engine(
                normalized,
                pool_pre_ping=True,
                pool_size=pool_size,
                max_overflow=max_overflow,
                echo=echo,
            )
        )

    async def init_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
