from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from hub.api.routers.admin.schemas import AdminDevice, AdminDeviceListResponse
from hub.api.routers.admin.service import build_admin_devices

router = APIRouter(prefix="/api/admin/devices", tags=["Admin Devices"])


@router.get("", response_model=AdminDeviceListResponse)
async def list_devices(
    request: Request,
    status: str | None = Query(default=None, description="Filter by online/degraded/offline"),
):
    runtime = request.app.state.admin_runtime
    device_manager = request.app.state.device_manager
    devices = await build_admin_devices(runtime=runtime, device_manager=device_manager)
    if status:
        devices = [device for device in devices if device.status == status]
    return AdminDeviceListResponse(devices=devices)


@router.get("/{device_id}", response_model=AdminDevice)
async def get_device(device_id: str, request: Request):
    runtime = request.app.state.admin_runtime
    device_manager = request.app.state.device_manager
    devices = await build_admin_devices(runtime=runtime, device_manager=device_manager)
    for device in devices:
        if device.device_id == device_id:
            return device
    raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
