"""HTTP interface for provider-neutral device directory and channel requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from fastapi import APIRouter, Header, HTTPException

from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.get_command import GetCommand
from hub.application.use_cases.provision_channel import (
    DeviceUnavailable,
    ProvisionChannel,
)
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.application.use_cases.send_command import SendCommand
from hub.contracts.bindings.channel import ChannelProvisionStatus
from hub.contracts.bindings.device import (
    DeviceApprovalRequest,
    DeviceBusEventPage,
    DeviceCommandRequest,
    DeviceCommandStatus,
    DeviceDirectoryEntry,
    DeviceLifecycleStatus,
    DeviceRevocationRequest,
)
from hub.contracts.mappers import (
    channel_status_to_wire,
    command_status_to_wire,
    directory_entry_to_wire,
    lifecycle_status_to_wire,
    stored_event_to_wire,
)
from hub.domain.channels.selection import ChannelProfileUnavailable
from hub.ports.event_bus import EventStreamReader
from hub.ports.identity import ManagementAuthorizer
from hub.ports.repositories import DeviceDirectoryRepository


@dataclass(frozen=True, slots=True)
class DeviceManagementHttpServices:
    directory: DeviceDirectoryRepository
    provision_channel: ProvisionChannel
    send_command: SendCommand
    get_command: GetCommand
    approve_device: ApproveDevice
    revoke_device: RevokeDevice
    authorizer: ManagementAuthorizer
    event_stream: EventStreamReader


def create_device_management_router(
    *, services: DeviceManagementHttpServices | Callable[[], DeviceManagementHttpServices]
) -> APIRouter:
    router = APIRouter(prefix="/api/device-management/v1", tags=["device-management"])

    def current() -> DeviceManagementHttpServices:
        return services() if callable(services) else services

    @router.get("/directory/{owner_scope}", response_model=list[DeviceDirectoryEntry])
    async def list_directory(owner_scope: str, authorization: str = Header(alias="Authorization")):
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization, owner_scope=owner_scope, device_id=None
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return [
            directory_entry_to_wire(entry)
            for entry in await runtime.directory.list(owner_scope=owner_scope)
        ]

    @router.get("/events/{owner_scope}", response_model=DeviceBusEventPage)
    async def list_device_events(
        owner_scope: str,
        after_stream_position: int = 0,
        limit: int = 100,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceBusEventPage:
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization, owner_scope=owner_scope, device_id=None
            )
            stored = await runtime.event_stream.list_after(
                owner_scope=owner_scope,
                stream_position=after_stream_position,
                limit=limit,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return DeviceBusEventPage(
            next_stream_position=(stored[-1].stream_position if stored else after_stream_position),
            events=tuple(stored_event_to_wire(item) for item in stored),
        )

    @router.post(
        "/devices/{device_id}/channels/{profile_name}",
        response_model=ChannelProvisionStatus,
    )
    async def request_channel(
        device_id: str,
        profile_name: str,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization, owner_scope=None, device_id=device_id
            )
            grant = await runtime.provision_channel.execute(
                device_id=device_id, profile_name=profile_name
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (DeviceUnavailable, ChannelProfileUnavailable) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Returning only non-secret lifecycle metadata prevents the HTTP
        # control client from becoming another channel-binding consumer.
        return channel_status_to_wire(grant)

    @router.post("/devices/{device_id}/commands", response_model=DeviceCommandStatus)
    async def send_device_command(
        device_id: str,
        payload: DeviceCommandRequest,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization, owner_scope=None, device_id=device_id
            )
            command = await runtime.send_command.execute(
                device_id=device_id,
                operation=payload.command_name,
                payload_json=payload.arguments_json,
                ttl=timedelta(milliseconds=payload.ttl_ms),
                request_id=payload.request_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except (ValueError, ConnectionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return command_status_to_wire(command)

    @router.get("/commands/{command_id}", response_model=DeviceCommandStatus)
    async def get_device_command(
        command_id: str,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            command = await runtime.get_command.execute(command_id)
            await runtime.authorizer.authorize(
                credential=authorization,
                owner_scope=None,
                device_id=command.device_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="command not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return command_status_to_wire(command)

    @router.post("/devices/{device_id}/approval", response_model=DeviceLifecycleStatus)
    async def approve_registered_device(
        device_id: str,
        payload: DeviceApprovalRequest,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization, owner_scope=None, device_id=device_id
            )
            device = await runtime.approve_device.execute(
                device_id=device_id,
                owner_id=payload.owner_id,
                request_id=payload.request_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return lifecycle_status_to_wire(device)

    @router.post("/devices/{device_id}/revocation", response_model=DeviceLifecycleStatus)
    async def revoke_registered_device(
        device_id: str,
        payload: DeviceRevocationRequest,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization, owner_scope=None, device_id=device_id
            )
            device = await runtime.revoke_device.execute(
                device_id=device_id,
                reason=payload.reason,
                request_id=payload.request_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return lifecycle_status_to_wire(device)

    return router
