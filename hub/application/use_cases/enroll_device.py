"""Challenge/proof enrollment creates an authenticated device session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from hub.domain.sessions.entities import DeviceSessionLease
from hub.ports.identity import (
    ChallengeRepository,
    Clock,
    CredentialIssuer,
    DeviceProofVerifier,
    EnrollmentChallenge,
    IdGenerator,
)
from hub.ports.repositories import DeviceAuthorityRepository, DeviceSessionRepository


@dataclass(frozen=True, slots=True)
class EnrollmentHello:
    device_id: str
    client_nonce: str


class EnrollDevice:
    def __init__(
        self,
        *,
        challenges: ChallengeRepository,
        sessions: DeviceSessionRepository,
        authority: DeviceAuthorityRepository,
        proof_verifier: DeviceProofVerifier,
        credential_issuer: CredentialIssuer,
        clock: Clock,
        ids: IdGenerator,
        hub_instance_id: str,
        challenge_ttl: timedelta = timedelta(seconds=30),
        session_ttl: timedelta = timedelta(seconds=45),
    ) -> None:
        self._challenges = challenges
        self._sessions = sessions
        self._authority = authority
        self._proof_verifier = proof_verifier
        self._credential_issuer = credential_issuer
        self._clock = clock
        self._ids = ids
        self._hub_instance_id = hub_instance_id
        self._challenge_ttl = challenge_ttl
        self._session_ttl = session_ttl

    async def begin(self, hello: EnrollmentHello) -> EnrollmentChallenge:
        if len(hello.client_nonce) < 16:
            raise ValueError("client_nonce must contain at least 16 characters")
        now = self._clock.now()
        challenge = EnrollmentChallenge(
            challenge_id=self._ids.new("challenge"),
            device_id=hello.device_id,
            client_nonce=hello.client_nonce,
            server_nonce=self._ids.new("nonce"),
            expires_at=now + self._challenge_ttl,
        )
        await self._challenges.create(challenge)
        return challenge

    async def complete(
        self,
        *,
        challenge_id: str,
        expected_device_id: str,
        public_key: str,
        signature: str,
    ) -> tuple[DeviceSessionLease, str]:
        challenge = await self._challenges.get(challenge_id)
        if challenge is None or challenge.consumed:
            raise PermissionError("unknown or consumed session challenge")
        now = self._clock.now()
        if challenge.expires_at <= now:
            raise PermissionError("session challenge expired")
        if challenge.device_id != expected_device_id:
            raise PermissionError("session challenge device mismatch")
        fingerprint = await self._proof_verifier.verify(
            challenge=challenge, public_key=public_key, signature=signature
        )
        authority = await self._authority.acquire(
            device_id=challenge.device_id,
            hub_instance_id=self._hub_instance_id,
            now=now,
            ttl=self._session_ttl,
        )
        await self._challenges.consume(challenge_id)
        session_id = self._ids.new("session")
        lease_token = self._credential_issuer.issue_lease_token(
            session_id=session_id, device_id=challenge.device_id
        )
        lease = DeviceSessionLease(
            session_id=session_id,
            device_id=challenge.device_id,
            opened_at=now,
            renewed_at=now,
            expires_at=now + self._session_ttl,
            lease_token=lease_token,
            identity_fingerprint=fingerprint,
            hub_instance_id=self._hub_instance_id,
            fencing_token=authority.fencing_token,
        )
        await self._sessions.upsert(lease)
        return lease, fingerprint
