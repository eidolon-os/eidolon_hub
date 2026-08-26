"""HTTP interface for the Hub device-management control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException

from hub.application.queries.get_device import GetDevice
from hub.contracts.bindings.device import (
    DeviceDirectoryEntry,
)
from hub.contracts.mappers import (
    directory_entry_to_wire,
)
from hub.ports.identity import ManagementAuthorizer, ManagementPermission


@dataclass(frozen=True, slots=True)
class DeviceManagementHttpServices:
    get_device: GetDevice
    authorizer: ManagementAuthorizer


def create_device_management_router(
    *, services: DeviceManagementHttpServices | Callable[[], DeviceManagementHttpServices]
) -> APIRouter:
    router = APIRouter(prefix="/api/device-management/v1", tags=["device-management"])

    def current() -> DeviceManagementHttpServices:
        return services() if callable(services) else services

    @router.get(
        "/owners/{owner_scope}/devices/{device_id}",
        response_model=DeviceDirectoryEntry,
    )
    async def get_directory_entry(
        owner_scope: str,
        device_id: str,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceDirectoryEntry:
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_GET,
                owner_scope=owner_scope,
                device_id=device_id,
            )
            entry = await runtime.get_device.execute(
                owner_scope=owner_scope,
                device_id=device_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return directory_entry_to_wire(entry)

    return router
