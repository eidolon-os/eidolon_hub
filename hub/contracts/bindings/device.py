"""Runtime normalization bindings for device wire contracts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel, DeviceIdentity, JsonObject


class PropertyAffordance(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    schema_: JsonObject = Field(alias="schema")
    observable: bool = False
    writable: bool = False


class ActionAffordance(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1, le=65_535)
    input_schema: JsonObject
    output_schema: JsonObject
    idempotent: bool = False


class EventAffordance(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    data_schema: JsonObject


class MediaCapability(ContractModel):
    kind: Literal["audio", "video"]
    direction: Literal["publish", "subscribe", "bidirectional"]
    codecs: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("codecs", mode="before")
    @classmethod
    def _codec_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceManifest(ContractModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=128)
    properties: tuple[PropertyAffordance, ...] = Field(default=(), max_length=64)
    actions: tuple[ActionAffordance, ...] = Field(default=(), max_length=64)
    events: tuple[EventAffordance, ...] = Field(default=(), max_length=64)
    media: tuple[MediaCapability, ...] = Field(default=(), max_length=16)

    @field_validator("properties", "actions", "events", "media", mode="before")
    @classmethod
    def _json_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceRegistration(ContractModel):
    operation: Literal["device.registration"] = "device.registration"
    request_id: str = Field(min_length=1, max_length=96)
    identity: DeviceIdentity
    manifest: DeviceManifest
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(default="unknown", min_length=1, max_length=96)


class DeviceCommandRequest(ContractModel):
    operation: Literal["device.command"] = "device.command"
    request_id: str = Field(min_length=1, max_length=96)
    command_name: str = Field(min_length=1, max_length=128)
    arguments_json: str = Field(min_length=2, max_length=262_144, repr=False)
    ttl_ms: int = Field(default=30_000, ge=1_000, le=300_000)

    @field_validator("arguments_json")
    @classmethod
    def _arguments_are_object(cls, value: str) -> str:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("arguments_json must contain valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("arguments_json must contain a JSON object")
        return value


class DeviceApprovalRequest(ContractModel):
    operation: Literal["device.approval"] = "device.approval"
    request_id: str = Field(min_length=1, max_length=96)
    owner_id: str = Field(min_length=1, max_length=64)


class DeviceRevocationRequest(ContractModel):
    operation: Literal["device.revocation"] = "device.revocation"
    request_id: str = Field(min_length=1, max_length=96)
    reason: str = Field(default="operator-request", min_length=1, max_length=256)


class DeviceRegistrationStatus(ContractModel):
    operation: Literal["device.registration-status"] = "device.registration-status"
    device_id: str = Field(min_length=1, max_length=128)
    manifest_revision: str = Field(min_length=1, max_length=128)
    approved: bool


class DeviceLifecycleStatus(ContractModel):
    operation: Literal["device.lifecycle-status"] = "device.lifecycle-status"
    device_id: str = Field(min_length=1, max_length=128)
    owner_id: str | None = Field(default=None, max_length=64)
    approved: bool
    revoked: bool


class DirectorySession(ContractModel):
    session_id: str = Field(min_length=1, max_length=128)
    expires_at: datetime


class DeviceDirectoryEntry(ContractModel):
    operation: Literal["device.directory-entry"] = "device.directory-entry"
    device_id: str = Field(min_length=1, max_length=128)
    owner_scope: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest_json: str = Field(min_length=2, max_length=262_144, repr=False)
    manifest_revision: str = Field(min_length=1, max_length=128)
    approved: bool
    revoked: bool
    online: bool
    sessions: tuple[DirectorySession, ...] = Field(default=(), max_length=32)
    registered_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)

    @field_validator("sessions", mode="before")
    @classmethod
    def _session_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceCommandStatus(ContractModel):
    operation: Literal["device.command-status"] = "device.command-status"
    command_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    command_name: str = Field(min_length=1, max_length=128)
    state: Literal[
        "queued", "sent", "accepted", "running", "succeeded", "failed", "rejected", "expired"
    ]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    error: str = Field(default="", max_length=4096)
    result_json: str | None = Field(default=None, max_length=262_144, repr=False)


class DeviceBusEvent(ContractModel):
    operation: Literal["device.bus-event"] = "device.bus-event"
    stream_position: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=512)
    device_id: str = Field(min_length=1, max_length=255)
    occurred_at: datetime
    data_json: str = Field(min_length=2, max_length=262_144, repr=False)


class DeviceBusEventPage(ContractModel):
    operation: Literal["device.bus-event-page"] = "device.bus-event-page"
    next_stream_position: int = Field(ge=0)
    events: tuple[DeviceBusEvent, ...] = Field(default=(), max_length=500)

    @field_validator("events", mode="before")
    @classmethod
    def _event_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ReportedState(ContractModel):
    operation: Literal["device.reported-state"] = "device.reported-state"
    device_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    observed_at_ms: int = Field(ge=0)
    values: JsonObject = Field(default_factory=dict, max_length=128)


class DeviceEvent(ContractModel):
    operation: Literal["device.event"] = "device.event"
    event_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    occurred_at_ms: int = Field(ge=0)
    payload: JsonObject = Field(default_factory=dict)
