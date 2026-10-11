"""Exercise the candidate's real startup migrations on an isolated SQLite copy."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from hub.adapters.persistence.database import HubDatabase
from hub.composition.resources import resolve_database_path
from hub.config import HubConfig


async def validate_startup(config: HubConfig) -> None:
    source = resolve_database_path(config)
    with TemporaryDirectory(prefix="eidolon-hub-preflight-") as directory:
        root = Path(directory)
        database_path = root / "hub.sqlite3"
        if source.exists():
            # SQLite backup includes committed WAL data without stopping the
            # running Hub. Opening read-only must never create a live database.
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as live:
                with sqlite3.connect(database_path) as copy:
                    live.backup(copy)

        def isolated(path: str | Path | None, name: str) -> Path | None:
            if path is None:
                return None
            target = root / name
            original = Path(path).expanduser().resolve()
            if original.exists():
                shutil.copyfile(original, target)
            return target

        anchor = config.persistence.authority_anchor_path
        if anchor is None:
            anchor = source.with_name(source.name + ".authority-lineage.json")
        database = HubDatabase.sqlite(
            database_path,
            owner_domain_id=config.onboarding.owner_domain_id,
            owner_domain_generation=config.onboarding.owner_domain_generation,
            authority_anchor_path=isolated(anchor, "anchor.json"),
            authority_bootstrap_path=isolated(
                config.persistence.authority_bootstrap_path, "bootstrap.json"
            ),
        )
        try:
            await database.initialize_schema()
            # A restart must also work after the migration has already run.
            await database.initialize_schema()
        finally:
            await database.close()
