"""Runtime normalization bindings for channel wire contracts."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel
from hub.contracts.bindings.device import DeviceManifest

ChannelKind = Literal["reliable-data", "realtime-data", "audio", "video"]


def _json_text(value: str, *, require_object: bool = False) -> str:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("field must contain valid JSON") from exc
    if require_object and not isinstance(decoded, dict):
        raise ValueError("field must contain a JSON object")
    return value


class CommandDataPayload(ContractModel):
    command_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    arguments_json: str = Field(min_length=2, max_length=262_144, repr=False)
    expires_at_ms: int = Field(ge=0)

    @field_validator("arguments_json")
    @classmethod
    def _arguments_are_object(cls, value: str) -> str:
        return _json_text(value, require_object=True)


class CommandAckPayload(ContractModel):
    command_id: str = Field(min_length=1, max_length=128)
    status: Literal["accepted", "running", "rejected"]
    error: str = Field(default="", max_length=1024)


class CommandResultPayload(ContractModel):
    command_id: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "rejected", "expired"]
    result_json: str | None = Field(default=None, max_length=262_144, repr=False)
    error: str = Field(default="", max_length=4096)

    @field_validator("result_json")
    @classmethod
    def _result_is_json(cls, value: str | None) -> str | None:
        return _json_text(value) if value is not None else None


class ReportedStatePayload(ContractModel):
    revision: int = Field(ge=1)
    values_json: str = Field(min_length=2, max_length=262_144, repr=False)

    @field_validator("values_json")
    @classmethod
    def _values_are_object(cls, value: str) -> str:
        return _json_text(value, require_object=True)


class DeviceEventPayload(ContractModel):
    event_id: str = Field(min_length=1, max_length=96)
    name: str = Field(min_length=1, max_length=128)
    payload_json: str = Field(min_length=2, max_length=262_144, repr=False)

    @field_validator("payload_json")
    @classmethod
    def _payload_is_object(cls, value: str) -> str:
        return _json_text(value, require_object=True)


class ProviderChannelDevice(ContractModel):
    device_id: str = Field(min_length=1, max_length=128)
    public_key_fingerprint: str = Field(min_length=1, max_length=512, repr=False)
    tenant_id: str = Field(min_length=1, max_length=128)
    owner_id: str | None = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=256)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest: DeviceManifest
    manifest_revision: str = Field(min_length=1, max_length=96)
    approved: bool
    revoked: bool
    connected: bool


class ProviderChannelAcquisitionRequest(ContractModel):
    operation: Literal["channel.acquire-device"] = "channel.acquire-device"
    operation_id: str = Field(min_length=1, max_length=128)
    hub_id: str = Field(min_length=1, max_length=128)
    device: ProviderChannelDevice


class ChannelAssignment(ContractModel):
    channel_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=96)
    kinds: tuple[ChannelKind, ...] = Field(min_length=1, max_length=4)
    binding_format: str = Field(min_length=1, max_length=128)
    issued_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    opaque_binding: str = Field(min_length=1, max_length=87_384, repr=False)

    @field_validator("kinds", mode="before")
    @classmethod
    def _kind_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("kinds")
    @classmethod
    def _unique_kinds(cls, value: tuple[ChannelKind, ...]) -> tuple[ChannelKind, ...]:
        if len(value) != len(set(value)):
            raise ValueError("channel kinds must be unique")
        return value


class ProviderChannelAcquisitionResponse(ContractModel):
    operation: Literal["channel.assignments"] = "channel.assignments"
    operation_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    manifest_revision: str = Field(min_length=1, max_length=96)
    channels: tuple[ChannelAssignment, ...] = Field(default=(), max_length=16)

    @field_validator("channels", mode="before")
    @classmethod
    def _channel_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ChannelAcquisitionRequest(ContractModel):
    operation: Literal["channel.acquire"] = "channel.acquire"
    request_id: str = Field(min_length=1, max_length=96)
    session_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)


class ChannelAcquisitionResponse(ContractModel):
    operation: Literal["channel.acquired"] = "channel.acquired"
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    manifest_revision: str = Field(min_length=1, max_length=96)
    channels: tuple[ChannelAssignment, ...] = Field(default=(), max_length=16)

    @field_validator("channels", mode="before")
    @classmethod
    def _channel_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ChannelLifecycleEvent(ContractModel):
    operation: Literal["channel.lifecycle"] = "channel.lifecycle"
    channel_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    state: Literal["active", "closed", "failed"]
    occurred_at_ms: int = Field(ge=0)
    reason: str = Field(default="", max_length=256)


class DataEnvelope(ContractModel):
    operation: Literal["channel.data"] = "channel.data"
    envelope_id: str = Field(min_length=1, max_length=96)
    channel_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    kind: Literal["command", "ack", "result", "state", "event"]
    sequence: int = Field(ge=1)
    occurred_at_ms: int = Field(ge=0)
    payload_json: str = Field(min_length=2, max_length=262_144, repr=False)
