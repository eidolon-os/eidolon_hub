"""Onboarding token, runtime and management authorization ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    """Authenticated management subject acting in an optional Owner namespace."""

    subject_id: str
    owner_id: str | None
    roles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.subject_id.strip() or len(self.subject_id) > 255:
            raise ValueError("management principal subject_id is invalid")
        if self.owner_id is not None and not self.owner_id.strip():
            raise ValueError("management principal owner_id is invalid")
        if not self.roles or any(not role.strip() for role in self.roles):
            raise ValueError("management principal roles are invalid")


class ManagementPermission(StrEnum):
    """One HTTP management operation authorized at the interface boundary."""

    DEVICE_LIST = "device:list"
    DEVICE_GET = "device:get"
    DEVICE_EVENTS = "device:events"
    DEVICE_APPROVE = "device:approve"
    DEVICE_PAIR_CLAIM = "device:pair-claim"
    DEVICE_REVOKE = "device:revoke"


class RetrievalTokenHasher(Protocol):
    def hash(self, token: str) -> str: ...
    def verify(self, token: str, token_hash: str) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


class ManagementAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        credential: str,
        permission: ManagementPermission,
        owner_scope: str | None,
        device_id: str | None,
    ) -> ManagementPrincipal: ...
