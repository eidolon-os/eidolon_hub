import sqlite3

import pytest

from hub.adapters.persistence.database import HubDatabase
from hub.composition.preflight import validate_startup
from hub.composition.resources import create_database
from hub.config import HubConfig, PersistenceConfig


async def test_startup_preflight_uses_live_wal_snapshot_and_never_changes_source(tmp_path, monkeypatch):
    source = tmp_path / "hub.sqlite3"
    config = HubConfig(persistence=PersistenceConfig(path=str(source)))
    db = create_database(config)
    await db.initialize_schema()
    await db.close()
    anchor = source.with_name(source.name + ".authority-lineage.json")
    original_anchor = anchor.read_bytes()
    original = HubDatabase.initialize_schema
    seen = []

    async def failing_migration(self):
        assert str(self.engine.url.database) != str(source)
        with sqlite3.connect(self.engine.url.database) as copy:
            assert copy.execute("select value from preflight_marker").fetchone() == (7,)
            copy.execute("update preflight_marker set value=9")
        self.authority_anchor_path.write_text("changed only in the copy")
        seen.append(self.engine.url.database)
        raise RuntimeError("candidate migration failed")

    with sqlite3.connect(source) as live:
        live.execute("pragma journal_mode=WAL")
        live.execute("create table preflight_marker(value integer)")
        live.execute("insert into preflight_marker values (7)")
        live.commit()
        monkeypatch.setattr(HubDatabase, "initialize_schema", failing_migration)
        with pytest.raises(RuntimeError, match="candidate migration failed"):
            await validate_startup(config)
        assert live.execute("select value from preflight_marker").fetchone() == (7,)
        assert anchor.read_bytes() == original_anchor
        live.execute("drop table preflight_marker")
        live.commit()
    assert seen
    monkeypatch.setattr(HubDatabase, "initialize_schema", original)
    await validate_startup(config)
    assert anchor.read_bytes() == original_anchor


async def test_first_install_preflight_does_not_create_live_state(tmp_path):
    path = tmp_path / "new.sqlite3"
    await validate_startup(HubConfig(persistence=PersistenceConfig(path=str(path))))
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
