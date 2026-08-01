"""Authentication and challenge persistence ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EnrollmentChallenge:
    challenge_id: str
    device_id: str
    client_nonce: str
    server_nonce: str
    expires_at: datetime
    connector_id: str
    connector_kind: str
    signaling_ref: str
    priority: int
    consumed: bool = False


class ChallengeRepository(Protocol):
    async def get(self, challenge_id: str) -> EnrollmentChallenge | None: ...
    async def create(self, challenge: EnrollmentChallenge) -> None: ...
    async def consume(self, challenge_id: str) -> EnrollmentChallenge: ...


class DeviceProofVerifier(Protocol):
    async def verify(
        self, *, challenge: EnrollmentChallenge, public_key: str, signature: str
    ) -> str: ...


class CredentialIssuer(Protocol):
    def issue_lease_token(self, *, connection_id: str, device_id: str) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


class ManagementAuthorizer(Protocol):
    async def authorize(
        self, *, credential: str, owner_scope: str | None, device_id: str | None
    ) -> None: ...
