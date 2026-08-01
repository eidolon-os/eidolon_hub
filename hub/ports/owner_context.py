"""Authorization context port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OwnerContext:
    owner_id: str
    active_companion_ids: frozenset[str]
    device_ids: frozenset[str]
    access_policy_json: str


class OwnerContextRepository(Protocol):
    async def get(self, owner_id: str) -> OwnerContext | None: ...
