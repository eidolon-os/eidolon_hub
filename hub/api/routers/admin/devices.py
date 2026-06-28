from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from hub.api.routers.admin.schemas import (
    AdminDevice,
    AdminDeviceListResponse,
    ApproveDeviceResponse,
    UnregisterDeviceResponse,
)
from hub.api.routers.admin.service import build_admin_devices, refresh_admin_devices

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


@router.post("/refresh", response_model=AdminDeviceListResponse)
async def refresh_devices(request: Request):
    """Force a LiveKit presence probe and return the current Hub device view.

    The device rows still come from Hub's persistent registry. Refresh only
    updates the runtime reachability overlay before composing the response.
    """
    runtime = request.app.state.admin_runtime
    device_manager = request.app.state.device_manager
    control_bridge = getattr(request.app.state, "control_bridge", None)
    config = getattr(request.app.state, "config", None)
    timeout = getattr(
        getattr(config, "admin", None),
        "command_timeout_seconds",
        None,
    )
    devices = await refresh_admin_devices(
        runtime=runtime,
        device_manager=device_manager,
        control_bridge=control_bridge,
        command_timeout_seconds=timeout,
    )
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
    LiveKit presence 不再创建设备记录;必须等设备至少完成一次
    ``GET /api/config`` 自动注册后才能批准.
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


@router.post("/{device_id}/enable", response_model=AdminDevice)
async def set_device_enabled(
    device_id: str,
    request: Request,
    enabled: bool = Query(default=True, description="Enable or disable the device"),
):
    """启用/禁用设备: 保留设备记录, 但改变是否允许它参与运行时使用."""
    runtime = request.app.state.admin_runtime
    device_manager = request.app.state.device_manager
    try:
        await device_manager.set_enabled(device_id, enabled=enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}") from exc
    devices = await build_admin_devices(runtime=runtime, device_manager=device_manager)
    for device in devices:
        if device.device_id == device_id:
            return device
    raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")


@router.delete("/{device_id}", response_model=UnregisterDeviceResponse)
async def unregister_device(device_id: str, request: Request):
    """注销设备: 从 hub 的持久记录中移除并清理 admin runtime 的 presence 缓存.

    幂等: 已经不存在的 device_id 也返回 200 (``existed=false``),便于
    调用方安全重试.

    清理范围 (仅 hub 内部):
        - device_manager._devices: registry DB backed 持久记录
        - admin_runtime._state: presence 缓存 (LiveKit 探测内存视图)

    不清理 (跨项目,admin 负责级联):
        - admin 项目内 device→agent binding (registry DB)
        - 任何运行时会话 (channel/livekit room 等)

    设备如果稍后通过 mDNS / GET /api/config 再次出现,会作为**全新**
    unapproved device 重新进入 discovery 流程,符合"显式批准"的设计。
    """
    device_manager = request.app.state.device_manager
    admin_runtime = request.app.state.admin_runtime
    existed = await device_manager.unregister(device_id)
    presence_existed = False
    if admin_runtime is not None:
        presence_existed = await admin_runtime.forget_presence(device_id)
    return UnregisterDeviceResponse(
        device_id=device_id,
        existed=existed,
        presence_cleared=presence_existed,
    )
