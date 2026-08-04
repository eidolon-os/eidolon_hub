"""Onboarding token, runtime and management authorization ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class RetrievalTokenHasher(Protocol):
    def hash(self, token: str) -> str: ...
    def verify(self, token: str, token_hash: str) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


class ManagementAuthorizer(Protocol):
    async def authorize(
        self, *, credential: str, owner_scope: str | None, device_id: str | None
    ) -> None: ...
