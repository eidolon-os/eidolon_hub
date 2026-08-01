"""Challenge/proof enrollment creates a protocol-independent connection lease."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.ports.identity import (
    ChallengeRepository,
    Clock,
    CredentialIssuer,
    DeviceProofVerifier,
    EnrollmentChallenge,
    IdGenerator,
)
from hub.ports.repositories import ConnectionRepository, DeviceAuthorityRepository


@dataclass(frozen=True, slots=True)
class EnrollmentHello:
    device_id: str
    connector_id: str
    connector_kind: ConnectorKind
    signaling_ref: str
    client_nonce: str
    priority: int = 100


class EnrollDevice:
    def __init__(
        self,
        *,
        challenges: ChallengeRepository,
        connections: ConnectionRepository,
        authority: DeviceAuthorityRepository,
        proof_verifier: DeviceProofVerifier,
        credential_issuer: CredentialIssuer,
        clock: Clock,
        ids: IdGenerator,
        hub_instance_id: str,
        challenge_ttl: timedelta = timedelta(seconds=30),
        connection_ttl: timedelta = timedelta(seconds=45),
    ) -> None:
        self._challenges = challenges
        self._connections = connections
        self._authority = authority
        self._proof_verifier = proof_verifier
        self._credential_issuer = credential_issuer
        self._clock = clock
        self._ids = ids
        self._hub_instance_id = hub_instance_id
        self._challenge_ttl = challenge_ttl
        self._connection_ttl = connection_ttl

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
            connector_id=hello.connector_id,
            connector_kind=hello.connector_kind.value,
            signaling_ref=hello.signaling_ref,
            priority=hello.priority,
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
    ) -> tuple[ConnectionLease, str]:
        challenge = await self._challenges.get(challenge_id)
        if challenge is None or challenge.consumed:
            raise PermissionError("unknown or consumed connection challenge")
        now = self._clock.now()
        if challenge.expires_at <= now:
            raise PermissionError("connection challenge expired")
        if challenge.device_id != expected_device_id:
            raise PermissionError("connection challenge device mismatch")
        fingerprint = await self._proof_verifier.verify(
            challenge=challenge, public_key=public_key, signature=signature
        )
        authority = await self._authority.acquire(
            device_id=challenge.device_id,
            hub_instance_id=self._hub_instance_id,
            now=now,
            ttl=self._connection_ttl,
        )
        await self._challenges.consume(challenge_id)
        connection_id = self._ids.new("connection")
        lease_token = self._credential_issuer.issue_lease_token(
            connection_id=connection_id, device_id=challenge.device_id
        )
        lease = ConnectionLease(
            connection_id=connection_id,
            device_id=challenge.device_id,
            connector_id=challenge.connector_id,
            connector_kind=ConnectorKind(challenge.connector_kind),
            signaling_ref=challenge.signaling_ref,
            opened_at=now,
            renewed_at=now,
            expires_at=now + self._connection_ttl,
            lease_token=lease_token,
            identity_fingerprint=fingerprint,
            hub_instance_id=self._hub_instance_id,
            fencing_token=authority.fencing_token,
            priority=challenge.priority,
        )
        await self._connections.upsert(lease)
        return lease, fingerprint
