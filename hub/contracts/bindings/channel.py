"""Runtime normalization bindings for channel wire contracts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel

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


class ChannelNegotiationSignal(ContractModel):
    operation: Literal["channel.offer", "channel.accept", "channel.close"]
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    connection_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    channel_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(default="", max_length=256)


class ChannelRequest(ContractModel):
    operation: Literal["channel.request"] = "channel.request"
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=96)
    required_kinds: tuple[ChannelKind, ...] = Field(min_length=1, max_length=4)
    expires_at_ms: int = Field(ge=0)

    @field_validator("required_kinds", mode="before")
    @classmethod
    def _kind_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ChannelGrant(ContractModel):
    operation: Literal["channel.grant"] = "channel.grant"
    request_id: str = Field(min_length=1, max_length=96)
    channel_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=96)
    lease_expires_at_ms: int = Field(ge=0)
    opaque_binding: str = Field(min_length=1, max_length=87_384, repr=False)


class ChannelLifecycleEvent(ContractModel):
    operation: Literal["channel.lifecycle"] = "channel.lifecycle"
    channel_id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "active", "renewing", "closed", "failed"]
    occurred_at_ms: int = Field(ge=0)
    reason: str = Field(default="", max_length=256)


class ChannelProvisionStatus(ContractModel):
    operation: Literal["channel.provision-status"] = "channel.provision-status"
    request_id: str = Field(min_length=1, max_length=96)
    channel_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=96)
    lease_expires_at: datetime
    grant_delivery: Literal["connection-signaling"] = "connection-signaling"


class DataEnvelope(ContractModel):
    operation: Literal["channel.data"] = "channel.data"
    envelope_id: str = Field(min_length=1, max_length=96)
    channel_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    kind: Literal["command", "ack", "result", "state", "event"]
    sequence: int = Field(ge=1)
    occurred_at_ms: int = Field(ge=0)
    payload_json: str = Field(min_length=2, max_length=262_144, repr=False)
