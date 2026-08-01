"""Internal event bus uses CloudEvents-compatible semantic fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DomainEvent:
    event_id: str
    event_type: str
    source: str
    subject: str
    occurred_at: datetime
    data_json: str


@dataclass(frozen=True, slots=True)
class StoredDomainEvent:
    stream_position: int
    event: DomainEvent


class EventBus(Protocol):
    async def publish(self, event: DomainEvent) -> None: ...


class EventStreamReader(Protocol):
    async def list_after(
        self, *, owner_scope: str, stream_position: int, limit: int
    ) -> tuple[StoredDomainEvent, ...]: ...
