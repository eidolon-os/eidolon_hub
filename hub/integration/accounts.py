"""Bound Provider accounts, per Owner. Never a credential: those are in the vault."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .store import IntegrationStore


@dataclass(frozen=True, slots=True)
class AccountRecord:
    account_id: str
    owner_id: str
    kind: str
    label: str
    status: str
    error: str | None = None
    choices: tuple[dict[str, str], ...] = field(default_factory=tuple)
    created_at_ms: int = 0
    last_seen_ms: int | None = None


class ProviderAccountStore:
    def __init__(self, store: IntegrationStore) -> None:
        self._store = store

    async def list(self, owner_id: str) -> list[AccountRecord]:
        with self._store.transaction() as db:
            cursor = db.execute(
                "SELECT * FROM provider_accounts WHERE owner_id=? ORDER BY created_at_ms, account_id",
                (owner_id,),
            )
            rows, names = cursor.fetchall(), [d[0] for d in cursor.description]
        return [_record(dict(zip(names, row, strict=True))) for row in rows]

    async def all(self) -> list[AccountRecord]:
        with self._store.transaction() as db:
            cursor = db.execute("SELECT * FROM provider_accounts ORDER BY created_at_ms")
            rows, names = cursor.fetchall(), [d[0] for d in cursor.description]
        return [_record(dict(zip(names, row, strict=True))) for row in rows]

    async def get(self, owner_id: str, account_id: str) -> AccountRecord | None:
        with self._store.transaction() as db:
            cursor = db.execute(
                "SELECT * FROM provider_accounts WHERE owner_id=? AND account_id=?",
                (owner_id, account_id),
            )
            row, names = cursor.fetchone(), [d[0] for d in cursor.description]
        return None if row is None else _record(dict(zip(names, row, strict=True)))

    async def upsert(self, record: AccountRecord) -> None:
        with self._store.transaction() as db:
            db.execute(
                """INSERT INTO provider_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id) DO UPDATE SET label=excluded.label,
                status=excluded.status, error=excluded.error, choices_json=excluded.choices_json,
                last_seen_ms=excluded.last_seen_ms""",
                (
                    record.account_id,
                    record.owner_id,
                    record.kind,
                    record.label,
                    record.status,
                    record.error,
                    json.dumps(list(record.choices), ensure_ascii=False),
                    record.created_at_ms or _now_ms(),
                    record.last_seen_ms,
                ),
            )

    async def set_status(
        self, account_id: str, status: str, *, error: str | None = None, seen: bool = False
    ) -> None:
        with self._store.transaction() as db:
            db.execute(
                "UPDATE provider_accounts SET status=?, error=?, last_seen_ms=COALESCE(?, last_seen_ms) "
                "WHERE account_id=?",
                (status, error, _now_ms() if seen else None, account_id),
            )

    async def delete(self, owner_id: str, account_id: str) -> bool:
        with self._store.transaction() as db:
            cursor = db.execute(
                "DELETE FROM provider_accounts WHERE owner_id=? AND account_id=?",
                (owner_id, account_id),
            )
            return cursor.rowcount > 0


def _record(row: dict) -> AccountRecord:
    return AccountRecord(
        account_id=row["account_id"],
        owner_id=row["owner_id"],
        kind=row["kind"],
        label=row["label"],
        status=row["status"],
        error=row["error"],
        choices=tuple(json.loads(row["choices_json"] or "[]")),
        created_at_ms=row["created_at_ms"],
        last_seen_ms=row["last_seen_ms"],
    )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
