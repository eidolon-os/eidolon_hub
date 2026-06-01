from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AdminDevice(BaseModel):
    device_id: str
    name: str = ""
    enabled: bool
    paired: bool
    # Phase 25: 操作员批准位 + 时间戳; 跟 paired 是独立维度.
    approved: bool = False
    approved_at: datetime | None = None
    last_seen: datetime | None = None
    status: str = "offline"
    room_name: str = ""
    participant_sid: str = ""
    missed_probes: int = 0


class AdminDeviceListResponse(BaseModel):
    devices: list[AdminDevice]


class ApproveDeviceResponse(BaseModel):
    """POST /devices/{id}/approve 的响应. 返回新状态以便前端直接更新视图."""
    device_id: str
    approved: bool
    approved_at: datetime | None


class UnregisterDeviceResponse(BaseModel):
    """DELETE /devices/{id} 的响应. 幂等接口,即使 device 不存在也返回 200,
    用 ``existed``/``presence_cleared`` 标志告诉调用方实际清理了什么."""

    device_id: str
    # True if a persistent record was removed from devices.json. False
    # means the device was already absent (idempotent retry / never
    # registered).
    existed: bool
    # True if a presence cache entry was cleared from admin_runtime.
    # Independent from ``existed``: a device with no persistent record
    # can still have a presence-only entry if it was seen by a LiveKit
    # probe before completing first-time registration.
    presence_cleared: bool


class CommandRequest(BaseModel):
    topic: str = Field(default="admin.command")
    payload: dict[str, Any] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    command_id: str
    device_id: str
    topic: str
    status: str
    created_at: str
    payload: dict[str, Any]
    updated_at: str
    error: str = ""


class CommandListResponse(BaseModel):
    commands: list[CommandResponse]


class ProbeHealthResponse(BaseModel):
    running: bool
    last_success_at: datetime | None
    last_error: str
    consecutive_failures: int
    total_cycles: int


class MetricsResponse(BaseModel):
    probe: dict[str, Any]
    devices: dict[str, Any]
    commands: dict[str, Any]
