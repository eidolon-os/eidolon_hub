from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AdminDevice(BaseModel):
    device_id: str
    name: str = ""
    enabled: bool
    paired: bool
    last_seen: datetime | None = None
    status: str = "offline"
    room_name: str = ""
    participant_sid: str = ""
    missed_probes: int = 0


class AdminDeviceListResponse(BaseModel):
    devices: list[AdminDevice]


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
