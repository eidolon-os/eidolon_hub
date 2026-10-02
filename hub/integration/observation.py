"""The last observed fact per target, and a stream of changes.

Observed, not asserted: a row says what a Provider last reported and when,
never what a command intended. ``seq`` is one counter per Owner, so a reader
holding (owner, seq) asks for everything after it; ``changes_since`` waits up
to a timeout for the next one, which is what a long poll needs.

``MemoryObservationCache`` keeps the same interface in memory for tests and
for a Hub without a state path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .store import IntegrationStore


@dataclass(frozen=True, slots=True)
class Observed:
    target: str
    reachable: bool
    state: dict[str, Any] | None
    observed_at_ms: int
    seq: int


class ObservationCache(Protocol):
    async def put(
        self,
        owner_id: str,
        target: str,
        *,
        reachable: bool,
        state: dict[str, Any] | None,
        observed_at_ms: int,
    ) -> Observed | None:
        """Record an observation; return it with its seq, or None when nothing changed."""
        ...

    async def forget(self, owner_id: str, targets: set[str]) -> None: ...

    async def snapshot(self, owner_id: str) -> dict[str, Observed]: ...

    async def changes_since(self, owner_id: str, seq: int, timeout_s: float) -> list[Observed]: ...

    async def latest_seq(self, owner_id: str) -> int: ...


class _Waiters:
    def __init__(self) -> None:
        self._conditions: dict[str, asyncio.Condition] = {}

    def condition(self, owner_id: str) -> asyncio.Condition:
        return self._conditions.setdefault(owner_id, asyncio.Condition())

    async def wake(self, owner_id: str) -> None:
        condition = self.condition(owner_id)
        async with condition:
            condition.notify_all()


class SqliteObservationCache:
    def __init__(self, store: IntegrationStore) -> None:
        self._store = store
        self._waiters = _Waiters()

    async def put(self, owner_id, target, *, reachable, state, observed_at_ms) -> Observed | None:
        state_json = (
            None
            if state is None
            else json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT reachable, state_json FROM observations WHERE owner_id=? AND target=?",
                (owner_id, target),
            ).fetchone()
            if row is not None and bool(row[0]) == reachable and row[1] == state_json:
                db.execute(
                    "UPDATE observations SET observed_at_ms=? WHERE owner_id=? AND target=?",
                    (observed_at_ms, owner_id, target),
                )
                return None
            seq = (
                db.execute(
                    "SELECT MAX(seq) FROM observations WHERE owner_id=?", (owner_id,)
                ).fetchone()[0]
                or 0
            ) + 1
            db.execute(
                """INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (owner_id, target) DO UPDATE SET reachable=excluded.reachable,
                state_json=excluded.state_json, observed_at_ms=excluded.observed_at_ms, seq=excluded.seq""",
                (owner_id, target, int(reachable), state_json, observed_at_ms, seq),
            )
        await self._waiters.wake(owner_id)
        return Observed(target, reachable, state, observed_at_ms, seq)

    async def forget(self, owner_id, targets) -> None:
        if not targets:
            return
        with self._store.transaction() as db:
            db.executemany(
                "DELETE FROM observations WHERE owner_id=? AND target=?",
                [(owner_id, target) for target in targets],
            )

    async def snapshot(self, owner_id) -> dict[str, Observed]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT target, reachable, state_json, observed_at_ms, seq FROM observations WHERE owner_id=?",
                (owner_id,),
            ).fetchall()
        return {row[0]: _observed(row) for row in rows}

    async def latest_seq(self, owner_id) -> int:
        with self._store.transaction() as db:
            return (
                db.execute(
                    "SELECT MAX(seq) FROM observations WHERE owner_id=?", (owner_id,)
                ).fetchone()[0]
                or 0
            )

    async def changes_since(self, owner_id, seq, timeout_s) -> list[Observed]:
        condition = self._waiters.condition(owner_id)
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            changes = self._after(owner_id, seq)
            if changes:
                return changes
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return []
            async with condition:
                try:
                    await asyncio.wait_for(condition.wait(), remaining)
                except TimeoutError:
                    return self._after(owner_id, seq)

    def _after(self, owner_id: str, seq: int) -> list[Observed]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT target, reachable, state_json, observed_at_ms, seq FROM observations "
                "WHERE owner_id=? AND seq>? ORDER BY seq",
                (owner_id, seq),
            ).fetchall()
        return [_observed(row) for row in rows]


def _observed(row) -> Observed:
    return Observed(
        row[0], bool(row[1]), None if row[2] is None else json.loads(row[2]), row[3], row[4]
    )


class MemoryObservationCache:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Observed]] = {}
        self._seq: dict[str, int] = {}
        self._waiters = _Waiters()

    async def put(self, owner_id, target, *, reachable, state, observed_at_ms) -> Observed | None:
        rows = self._rows.setdefault(owner_id, {})
        current = rows.get(target)
        if current is not None and current.reachable == reachable and current.state == state:
            rows[target] = Observed(target, reachable, state, observed_at_ms, current.seq)
            return None
        seq = self._seq.get(owner_id, 0) + 1
        self._seq[owner_id] = seq
        rows[target] = observed = Observed(
            target, reachable, None if state is None else dict(state), observed_at_ms, seq
        )
        await self._waiters.wake(owner_id)
        return observed

    async def forget(self, owner_id, targets) -> None:
        rows = self._rows.get(owner_id, {})
        for target in targets:
            rows.pop(target, None)

    async def snapshot(self, owner_id) -> dict[str, Observed]:
        return dict(self._rows.get(owner_id, {}))

    async def latest_seq(self, owner_id) -> int:
        return self._seq.get(owner_id, 0)

    async def changes_since(self, owner_id, seq, timeout_s) -> list[Observed]:
        condition = self._waiters.condition(owner_id)
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            changes = sorted(
                (o for o in self._rows.get(owner_id, {}).values() if o.seq > seq),
                key=lambda o: o.seq,
            )
            if changes:
                return changes
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return []
            async with condition:
                try:
                    await asyncio.wait_for(condition.wait(), remaining)
                except TimeoutError:
                    return sorted(
                        (o for o in self._rows.get(owner_id, {}).values() if o.seq > seq),
                        key=lambda o: o.seq,
                    )
