from __future__ import annotations

from datetime import datetime
from typing import Any

from eidolon_sdk.biz.contracts import CONTROL_TOPIC
from pydantic import BaseModel, Field


class AdminDevice(BaseModel):
    device_id: str
    name: str = ""
    kind: str = "unknown"
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


class DiscoveryStatusResponse(BaseModel):
    service_type: str
    service_name: str
    hostname: str
    port: int
    registered: bool
    ip: str = ""
    config_url: str = ""
    last_registered_at: str | None = None
    last_updated_at: str | None = None
    last_error: str = ""


class ApproveDeviceResponse(BaseModel):
    """POST /devices/{id}/approve 的响应. 返回新状态以便前端直接更新视图."""
    device_id: str
    approved: bool
    approved_at: datetime | None


class UnregisterDeviceResponse(BaseModel):
    """DELETE /devices/{id} 的响应. 幂等接口,即使 device 不存在也返回 200,
    用 ``existed``/``presence_cleared`` 标志告诉调用方实际清理了什么."""

    device_id: str
    # True if a persistent registry record was removed. False
    # means the device was already absent (idempotent retry / never
    # registered).
    existed: bool
    # True if a presence cache entry was cleared from admin_runtime.
    # Independent from ``existed``: the cache can temporarily retain a
    # previously registered device until the next LiveKit probe cycle.
    presence_cleared: bool


class CommandRequest(BaseModel):
    topic: str = Field(default=CONTROL_TOPIC)
    op: str | None = Field(default=None)
    payload: dict[str, Any] = Field(default_factory=dict)
    ttl_ms: int = Field(default=30_000, ge=1_000, le=600_000)
    qos: str = Field(default="ack", pattern="^(fire_and_forget|ack|result)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")


class CommandResponse(BaseModel):
    command_id: str
    device_id: str
    topic: str
    op: str = ""
    status: str
    created_at: str
    payload: dict[str, Any]
    envelope: dict[str, Any] = Field(default_factory=dict)
    ttl_ms: int = 30_000
    qos: str = "ack"
    priority: str = "normal"
    updated_at: str
    error: str = ""
    ack: dict[str, Any] | None = None
    result: Any = None


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
