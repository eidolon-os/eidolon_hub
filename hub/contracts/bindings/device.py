"""Runtime normalization bindings for device wire contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel, JsonObject

DeviceLifecycleState = Literal["pending-approval", "approved", "revoked"]


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


class DeviceApprovalRequest(ContractModel):
    operation: Literal["device.approval"] = "device.approval"
    request_id: str = Field(min_length=1, max_length=96)
    owner_id: str = Field(min_length=1, max_length=64)


class DeviceRevocationRequest(ContractModel):
    operation: Literal["device.revocation"] = "device.revocation"
    request_id: str = Field(min_length=1, max_length=96)
    reason: str = Field(default="operator-request", min_length=1, max_length=256)


class DeviceLifecycleStatus(ContractModel):
    operation: Literal["device.lifecycle-status"] = "device.lifecycle-status"
    device_id: str = Field(min_length=1, max_length=128)
    owner_id: str | None = Field(default=None, max_length=64)
    lifecycle_state: DeviceLifecycleState


class DeviceDirectoryEntry(ContractModel):
    operation: Literal["device.directory-entry"] = "device.directory-entry"
    device_id: str = Field(min_length=1, max_length=128)
    owner_scope: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest: DeviceManifest
    manifest_revision: str = Field(min_length=1, max_length=128)
    lifecycle_state: DeviceLifecycleState
    enrolled_at: datetime
    updated_at: datetime


class DeviceDirectoryPage(ContractModel):
    operation: Literal["device.directory-page"] = "device.directory-page"
    next_cursor: str | None = Field(default=None, max_length=128)
    devices: tuple[DeviceDirectoryEntry, ...] = Field(default=(), max_length=100)

    @field_validator("devices", mode="before")
    @classmethod
    def _device_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceManagementEvent(ContractModel):
    operation: Literal["device.management-event"] = "device.management-event"
    stream_position: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=512)
    device_id: str = Field(min_length=1, max_length=255)
    occurred_at: datetime
    data: JsonObject


class DeviceManagementEventPage(ContractModel):
    operation: Literal["device.management-event-page"] = "device.management-event-page"
    next_stream_position: int = Field(ge=0)
    events: tuple[DeviceManagementEvent, ...] = Field(default=(), max_length=500)

    @field_validator("events", mode="before")
    @classmethod
    def _event_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value
