from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from hub.api.routers.admin.schemas import (
    AdminDevice,
    AdminDeviceListResponse,
    ApproveDeviceResponse,
)
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


@router.post("/{device_id}/approve", response_model=ApproveDeviceResponse)
async def approve_device(device_id: str, request: Request):
    """操作员批准设备进入系统.

    幂等: 重复调返回相同状态. 必须先在 device_manager 里有记录 ——
    presence-only 设备 (出现在 room 但还没注册) 不能直接批准,
    要等设备至少完成一次 ``GET /api/config`` 自动注册才能批.
    """
    device_manager = request.app.state.device_manager
    if device_manager.get(device_id) is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Device not registered: {device_id} — wait for the device to "
                "call GET /api/config at least once before approving."
            ),
        )
    device = await device_manager.approve(device_id)
    return ApproveDeviceResponse(
        device_id=device.device_id,
        approved=device.approved,
        approved_at=device.approved_at,
    )
