"""Durable receipts: one row per (owner, scope, request), so a repeat after a
restart still answers with the first result and a conflicting reuse is still
refused. Four timestamps split a command's time into Hub, Provider and
confirmation segments for acceptance measurements.

``MemoryLedger`` has the same interface for tests and for a Hub with no state
path; it forgets on restart, which is what the in-process cache did before.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from .store import IntegrationStore

# A repeat this long after the first submission still answers with the first result.
IDEMPOTENCY_WINDOW_MS = 24 * 60 * 60_000
# Rows older than this are pruned on the next begin; they are evidence, not state.
RETENTION_MS = 7 * 24 * 60 * 60_000


@dataclass(frozen=True, slots=True)
class Timestamps:
    submitted_at_ms: int
    provider_started_at_ms: int | None = None
    provider_returned_at_ms: int | None = None
    confirmed_at_ms: int | None = None
    completed_at_ms: int | None = None
    reconciled_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Receipt:
    owner_id: str
    scope: str
    request_id: str
    fingerprint: str
    timestamps: Timestamps
    result: dict[str, Any] | None  # None while in flight


class ReceiptConflict(ValueError):
    """The same request id was reused for a different request."""


class ReceiptLedger(Protocol):
    async def begin(
        self, owner_id: str, scope: str, request_id: str, fingerprint: str, now_ms: int
    ) -> Receipt | None:
        """Record a new submission and return None; or return the existing receipt.

        Raises ``ReceiptConflict`` when the fingerprint differs from the recorded one.
        """
        ...

    async def complete(
        self,
        owner_id: str,
        scope: str,
        request_id: str,
        result: dict[str, Any],
        timestamps: Timestamps,
    ) -> None: ...

    async def get(self, owner_id: str, scope: str, request_id: str) -> Receipt | None: ...

    async def reconcile(
        self,
        owner_id: str,
        scope: str,
        request_id: str,
        target: str,
        result_item: dict[str, Any],
        at_ms: int,
    ) -> bool:
        """Replace the recorded item for ``target`` once a later observation settled it.

        Returns False when there is no such receipt or the item is no longer unknown.
        """
        ...


class SqliteReceiptLedger:
    def __init__(self, store: IntegrationStore) -> None:
        self._store = store

    async def begin(self, owner_id, scope, request_id, fingerprint, now_ms) -> Receipt | None:
        with self._store.transaction() as db:
            db.execute("DELETE FROM receipts WHERE submitted_at_ms < ?", (now_ms - RETENTION_MS,))
            row = _fetch(db, owner_id, scope, request_id)
            if row is not None and row.timestamps.submitted_at_ms >= now_ms - IDEMPOTENCY_WINDOW_MS:
                if row.fingerprint != fingerprint:
                    raise ReceiptConflict(request_id)
                return row
            db.execute(
                """INSERT INTO receipts (owner_id, scope, request_id, fingerprint, submitted_at_ms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner_id, scope, request_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                submitted_at_ms=excluded.submitted_at_ms, provider_started_at_ms=NULL,
                provider_returned_at_ms=NULL, confirmed_at_ms=NULL, completed_at_ms=NULL, result_json=NULL""",
                (owner_id, scope, request_id, fingerprint, now_ms),
            )
        return None

    async def complete(self, owner_id, scope, request_id, result, timestamps) -> None:
        with self._store.transaction() as db:
            db.execute(
                """UPDATE receipts SET provider_started_at_ms=?, provider_returned_at_ms=?,
                confirmed_at_ms=?, completed_at_ms=?, result_json=?
                WHERE owner_id=? AND scope=? AND request_id=?""",
                (
                    timestamps.provider_started_at_ms,
                    timestamps.provider_returned_at_ms,
                    timestamps.confirmed_at_ms,
                    timestamps.completed_at_ms,
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    owner_id,
                    scope,
                    request_id,
                ),
            )

    async def get(self, owner_id, scope, request_id) -> Receipt | None:
        with self._store.transaction() as db:
            return _fetch(db, owner_id, scope, request_id)

    async def reconcile(self, owner_id, scope, request_id, target, result_item, at_ms) -> bool:
        with self._store.transaction() as db:
            receipt = _fetch(db, owner_id, scope, request_id)
            replaced = _replace_unknown(receipt, target, result_item)
            if replaced is None:
                return False
            db.execute(
                "UPDATE receipts SET result_json=?, reconciled_at_ms=? WHERE owner_id=? AND scope=? AND request_id=?",
                (
                    json.dumps(replaced, ensure_ascii=False, separators=(",", ":")),
                    at_ms,
                    owner_id,
                    scope,
                    request_id,
                ),
            )
            return True


def _replace_unknown(
    receipt: Receipt | None, target: str, result_item: dict[str, Any]
) -> dict[str, Any] | None:
    if receipt is None or receipt.result is None:
        return None
    items = list(receipt.result.get("results") or [])
    for index, item in enumerate(items):
        if item.get("device_id") == target and item.get("status") == "unknown":
            items[index] = result_item
            return {**receipt.result, "results": items}
    return None


def _fetch(db, owner_id: str, scope: str, request_id: str) -> Receipt | None:
    row = db.execute(
        """SELECT fingerprint, submitted_at_ms, provider_started_at_ms, provider_returned_at_ms,
        confirmed_at_ms, completed_at_ms, result_json, reconciled_at_ms FROM receipts
        WHERE owner_id=? AND scope=? AND request_id=?""",
        (owner_id, scope, request_id),
    ).fetchone()
    if row is None:
        return None
    return Receipt(
        owner_id=owner_id,
        scope=scope,
        request_id=request_id,
        fingerprint=row[0],
        timestamps=Timestamps(row[1], row[2], row[3], row[4], row[5], row[7]),
        result=None if row[6] is None else json.loads(row[6]),
    )


class MemoryLedger:
    def __init__(self, capacity: int = 1024) -> None:
        self._rows: OrderedDict[tuple[str, str, str], Receipt] = OrderedDict()
        self._capacity = capacity

    async def begin(self, owner_id, scope, request_id, fingerprint, now_ms) -> Receipt | None:
        key = (owner_id, scope, request_id)
        row = self._rows.get(key)
        if row is not None and row.timestamps.submitted_at_ms >= now_ms - IDEMPOTENCY_WINDOW_MS:
            if row.fingerprint != fingerprint:
                raise ReceiptConflict(request_id)
            return row
        self._rows[key] = Receipt(
            owner_id, scope, request_id, fingerprint, Timestamps(now_ms), None
        )
        while len(self._rows) > self._capacity:
            self._rows.popitem(last=False)
        return None

    async def complete(self, owner_id, scope, request_id, result, timestamps) -> None:
        key = (owner_id, scope, request_id)
        row = self._rows.get(key)
        if row is not None:
            self._rows[key] = Receipt(
                owner_id, scope, request_id, row.fingerprint, timestamps, result
            )

    async def get(self, owner_id, scope, request_id) -> Receipt | None:
        return self._rows.get((owner_id, scope, request_id))

    async def reconcile(self, owner_id, scope, request_id, target, result_item, at_ms) -> bool:
        key = (owner_id, scope, request_id)
        row = self._rows.get(key)
        replaced = _replace_unknown(row, target, result_item)
        if replaced is None:
            return False
        timestamps = Timestamps(
            row.timestamps.submitted_at_ms,
            row.timestamps.provider_started_at_ms,
            row.timestamps.provider_returned_at_ms,
            row.timestamps.confirmed_at_ms,
            row.timestamps.completed_at_ms,
            at_ms,
        )
        self._rows[key] = Receipt(
            owner_id, scope, request_id, row.fingerprint, timestamps, replaced
        )
        return True


def now_ms() -> int:
    return time.time_ns() // 1_000_000
