"""FastAPI HTTPS binding for the connection lifecycle contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException

from hub.application.use_cases.authenticate_connection import AuthenticateConnection
from hub.application.use_cases.enroll_device import EnrollDevice, EnrollmentHello
from hub.application.use_cases.handle_channel_signal import HandleChannelSignal
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_connection import RenewConnection
from hub.contracts.bindings.channel import ChannelNegotiationSignal
from hub.contracts.bindings.connection import (
    ChannelSignalAccepted,
    ChannelSignalDelivery,
    ConnectionAccepted,
    ConnectionChallenge,
    ConnectionHeartbeat,
    ConnectionHello,
    ConnectionProof,
    ConnectionRegistration,
    HubDescriptor,
)
from hub.contracts.bindings.device import DeviceRegistrationStatus
from hub.contracts.mappers import (
    channel_signal_to_domain,
    registration_status_to_wire,
    registration_to_domain,
)
from hub.domain.connections.entities import ConnectorKind


class HttpSignalMailbox:
    """Bounded per-device queue used for HTTPS long-poll channel signaling."""

    def __init__(self, *, max_pending_per_device: int = 32) -> None:
        self._queues: dict[str, asyncio.Queue[bytes]] = {}
        self._max_pending = max_pending_per_device

    def signaling_ref(self, device_id: str) -> str:
        return f"http-mailbox:{device_id}"

    async def put(self, *, signaling_ref: str, payload: bytes) -> None:
        prefix = "http-mailbox:"
        if not signaling_ref.startswith(prefix):
            raise ValueError("not an HTTP mailbox signaling reference")
        device_id = signaling_ref[len(prefix) :]
        queue = self._queues.setdefault(device_id, asyncio.Queue(maxsize=self._max_pending))
        queue.put_nowait(payload)

    async def poll(self, device_id: str, *, timeout_seconds: float) -> bytes | None:
        queue = self._queues.setdefault(device_id, asyncio.Queue(maxsize=self._max_pending))
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None


@dataclass(frozen=True, slots=True)
class HttpConnectionServices:
    descriptor: HubDescriptor
    enroll: EnrollDevice
    register: RegisterDevice
    renew: RenewConnection
    mailbox: HttpSignalMailbox
    authenticate_connection: AuthenticateConnection
    handle_channel_signal: HandleChannelSignal
    heartbeat_after_ms: int = 15_000
    connector_id: str = "https-local"


def create_connection_router(
    services: HttpConnectionServices | Callable[[], HttpConnectionServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/connection/v1", tags=["connection"])

    def current() -> HttpConnectionServices:
        return services() if callable(services) else services

    @router.get("/descriptor", response_model=HubDescriptor)
    async def descriptor() -> HubDescriptor:
        return current().descriptor

    @router.post("/hello", response_model=ConnectionChallenge)
    async def hello(payload: ConnectionHello) -> ConnectionChallenge:
        runtime = current()
        try:
            if payload.connector_id != runtime.connector_id:
                raise ValueError("connection hello connector_id does not match binding")
            challenge = await runtime.enroll.begin(
                EnrollmentHello(
                    device_id=payload.device_id,
                    connector_id=runtime.connector_id,
                    connector_kind=ConnectorKind.HTTPS,
                    signaling_ref=runtime.mailbox.signaling_ref(payload.device_id),
                    client_nonce=payload.client_nonce,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ConnectionChallenge(
            request_id=payload.request_id,
            challenge_id=challenge.challenge_id,
            server_nonce=challenge.server_nonce,
            expires_at_ms=int(challenge.expires_at.timestamp() * 1000),
        )

    @router.post("/proof", response_model=ConnectionAccepted)
    async def proof(payload: ConnectionProof) -> ConnectionAccepted:
        runtime = current()
        try:
            lease, _ = await runtime.enroll.complete(
                challenge_id=payload.challenge_id,
                expected_device_id=payload.device_id,
                public_key=payload.public_key,
                signature=payload.signature,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return ConnectionAccepted(
            request_id=payload.request_id,
            connection_id=lease.connection_id,
            lease_token=lease.lease_token,
            lease_expires_at_ms=int(lease.expires_at.timestamp() * 1000),
            heartbeat_after_ms=runtime.heartbeat_after_ms,
        )

    @router.post("/register", response_model=DeviceRegistrationStatus)
    async def register(payload: ConnectionRegistration) -> DeviceRegistrationStatus:
        runtime = current()
        try:
            device = await runtime.register.execute(
                connection_id=payload.connection_id,
                lease_token=payload.lease_token,
                registration=registration_to_domain(payload.registration),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return registration_status_to_wire(device)

    @router.post("/heartbeat", response_model=ConnectionAccepted)
    async def heartbeat(payload: ConnectionHeartbeat) -> ConnectionAccepted:
        runtime = current()
        try:
            lease = await runtime.renew.execute(
                connection_id=payload.connection_id,
                lease_token=payload.lease_token,
                sequence=payload.sequence,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="connection not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return ConnectionAccepted(
            request_id=payload.request_id,
            connection_id=lease.connection_id,
            lease_token=lease.lease_token,
            lease_expires_at_ms=int(lease.expires_at.timestamp() * 1000),
            heartbeat_after_ms=runtime.heartbeat_after_ms,
            registration_required=False,
        )

    @router.get("/signals/{device_id}", response_model=ChannelSignalDelivery)
    async def poll_signal(
        device_id: str,
        connection_id: str,
        lease_token: str,
        timeout_seconds: float = 20.0,
    ) -> ChannelSignalDelivery:
        runtime = current()
        try:
            await runtime.authenticate_connection.execute(
                connection_id=connection_id,
                device_id=device_id,
                lease_token=lease_token,
            )
        except PermissionError:
            raise HTTPException(status_code=401, detail="active connection lease required")
        timeout = min(max(timeout_seconds, 0.1), 30.0)
        payload = await runtime.mailbox.poll(device_id, timeout_seconds=timeout)
        return ChannelSignalDelivery(payload_json=payload.decode() if payload is not None else None)

    @router.post("/signals", response_model=ChannelSignalAccepted)
    async def submit_signal(payload: ChannelNegotiationSignal) -> ChannelSignalAccepted:
        runtime = current()
        try:
            await runtime.authenticate_connection.execute(
                connection_id=payload.connection_id,
                device_id=payload.device_id,
                lease_token=payload.lease_token,
            )
            await runtime.handle_channel_signal.execute(channel_signal_to_domain(payload))
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return ChannelSignalAccepted(request_id=payload.request_id)

    return router
