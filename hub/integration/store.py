"""One SQLite file for the integration primitives, with the schema they share."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_accounts (
    account_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    choices_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    last_seen_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_provider_accounts_owner ON provider_accounts (owner_id);
CREATE TABLE IF NOT EXISTS credentials (
    account_id TEXT NOT NULL,
    key TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, key)
);
CREATE TABLE IF NOT EXISTS receipts (
    owner_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    submitted_at_ms INTEGER NOT NULL,
    provider_started_at_ms INTEGER,
    provider_returned_at_ms INTEGER,
    confirmed_at_ms INTEGER,
    completed_at_ms INTEGER,
    result_json TEXT,
    reconciled_at_ms INTEGER,
    PRIMARY KEY (owner_id, scope, request_id)
);
CREATE INDEX IF NOT EXISTS ix_receipts_submitted ON receipts (submitted_at_ms);
CREATE TABLE IF NOT EXISTS observations (
    owner_id TEXT NOT NULL,
    target TEXT NOT NULL,
    reachable INTEGER NOT NULL,
    state_json TEXT,
    observed_at_ms INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (owner_id, target)
);
CREATE INDEX IF NOT EXISTS ix_observations_owner_seq ON observations (owner_id, seq);
"""


class IntegrationStore:
    """Opens the file, creates the tables once, hands out short transactions."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported integration store schema version: {version}")
            db.executescript(_SCHEMA)
            if version == 1:
                # v2: a receipt remembers when a later observation settled an unknown outcome.
                db.execute("ALTER TABLE receipts ADD COLUMN reconciled_at_ms INTEGER")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Credentials live here; only the service account reads them.
        os.chmod(self._path, 0o600)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            db.commit()
        finally:
            db.close()
