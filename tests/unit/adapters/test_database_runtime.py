from __future__ import annotations

import pytest

from hub.adapters.persistence.database import HubDatabase


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
