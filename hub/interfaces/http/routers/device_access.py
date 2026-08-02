"""HTTPS interface for bootstrap, device sessions and channel acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException

from hub.application.use_cases.acquire_device_channels import AcquireDeviceChannels
from hub.application.use_cases.close_session import CloseDeviceSession
from hub.application.use_cases.enroll_device import EnrollDevice, EnrollmentHello
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_session import RenewDeviceSession
from hub.contracts.bindings.channel import (
    ChannelAcquisitionRequest,
    ChannelAcquisitionResponse,
)
from hub.contracts.bindings.device import DeviceRegistrationStatus
from hub.contracts.bindings.session import (
    HubDescriptor,
    SessionAccepted,
    SessionChallenge,
    SessionClosed,
    SessionHeartbeat,
    SessionHello,
    SessionProof,
    SessionRegistration,
)
from hub.contracts.mappers import (
    channel_assignments_to_wire,
    registration_status_to_wire,
    registration_to_domain,
)
from hub.domain.sessions.entities import DeviceSessionLease


@dataclass(frozen=True, slots=True)
class DeviceAccessHttpServices:
    descriptor: HubDescriptor
    enroll: EnrollDevice
    register: RegisterDevice
    renew: RenewDeviceSession
    close: CloseDeviceSession
    acquire_channels: AcquireDeviceChannels
    heartbeat_after_ms: int = 15_000


def create_device_access_router(
    services: DeviceAccessHttpServices | Callable[[], DeviceAccessHttpServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/device-access/v1", tags=["device-access"])

    def current() -> DeviceAccessHttpServices:
        return services() if callable(services) else services

    @router.get("/descriptor", response_model=HubDescriptor)
    async def descriptor() -> HubDescriptor:
        return current().descriptor

    @router.post("/hello", response_model=SessionChallenge)
    async def hello(payload: SessionHello) -> SessionChallenge:
        try:
            challenge = await current().enroll.begin(
                EnrollmentHello(
                    device_id=payload.device_id,
                    client_nonce=payload.client_nonce,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return SessionChallenge(
            request_id=payload.request_id,
            challenge_id=challenge.challenge_id,
            server_nonce=challenge.server_nonce,
            expires_at_ms=int(challenge.expires_at.timestamp() * 1000),
        )

    @router.post("/proof", response_model=SessionAccepted)
    async def proof(payload: SessionProof) -> SessionAccepted:
        try:
            lease, _ = await current().enroll.complete(
                challenge_id=payload.challenge_id,
                expected_device_id=payload.device_id,
                public_key=payload.public_key,
                signature=payload.signature,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return _accepted(payload.request_id, lease, current().heartbeat_after_ms)

    @router.post("/register", response_model=DeviceRegistrationStatus)
    async def register(payload: SessionRegistration) -> DeviceRegistrationStatus:
        try:
            device = await current().register.execute(
                session_id=payload.session_id,
                lease_token=payload.lease_token,
                registration=registration_to_domain(payload.registration),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return registration_status_to_wire(device)

    @router.post("/heartbeat", response_model=SessionAccepted)
    async def heartbeat(payload: SessionHeartbeat) -> SessionAccepted:
        try:
            lease = await current().renew.execute(
                session_id=payload.session_id,
                lease_token=payload.lease_token,
                sequence=payload.sequence,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device session not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _accepted(
            payload.request_id,
            lease,
            current().heartbeat_after_ms,
            registration_required=False,
        )

    @router.post("/close", response_model=SessionClosed)
    async def close(payload: SessionClosed) -> SessionClosed:
        try:
            await current().close.execute(
                session_id=payload.session_id,
                device_id=payload.device_id,
                lease_token=payload.lease_token,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device session not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return payload

    @router.post("/channels/acquire", response_model=ChannelAcquisitionResponse)
    async def acquire_channels(
        payload: ChannelAcquisitionRequest,
    ) -> ChannelAcquisitionResponse:
        try:
            assignments = await current().acquire_channels.execute(
                session_id=payload.session_id,
                lease_token=payload.lease_token,
                request_id=payload.request_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return channel_assignments_to_wire(assignments, request_id=payload.request_id)

    return router


def _accepted(
    request_id: str,
    lease: DeviceSessionLease,
    heartbeat_after_ms: int,
    *,
    registration_required: bool = True,
) -> SessionAccepted:
    return SessionAccepted(
        request_id=request_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        lease_expires_at_ms=int(lease.expires_at.timestamp() * 1000),
        heartbeat_after_ms=heartbeat_after_ms,
        registration_required=registration_required,
    )
