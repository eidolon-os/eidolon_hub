"""Strict short-lived owner-scoped device-event contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

EVENT_SCHEMA_VERSION = 1
EVENT_DEFAULT_TTL_MS = 3_000
EVENT_MAX_CLOCK_SKEW_MS = 1_000
EVENT_MAX_BYTES = 2_048

AMBIENT_PRESENCE_STATE_TYPE = "ambient.presence.state"
IDENTITY_OWNER_PRESENCE_CONFIRMED_TYPE = "identity.owner_presence.confirmed"
IDENTITY_OWNER_PRESENCE_CHANGED_TYPE = "identity.owner_presence.changed"
COMPANION_FLOW_NODE_TYPE = "companion.flow.node"

_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class EventSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    device_id: str = Field(min_length=1, max_length=128)
    component: str = Field(min_length=1, max_length=64)

    @field_validator("component")
    @classmethod
    def _component_is_namespaced(cls, value: str) -> str:
        if _COMPONENT_RE.fullmatch(value) is None:
            raise ValueError("component must be a lowercase namespaced identifier")
        return value


class AmbientPresenceStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: Literal["present", "vacant"]
    modality: Literal["mmwave"] = "mmwave"
    presence_epoch: int = Field(ge=1, le=4_294_967_295)
    sequence: int = Field(ge=1, le=4_294_967_295)
    lease_ms: int = Field(ge=0, le=60_000)
    observation: Literal["edge", "snapshot", "heartbeat"]

    @model_validator(mode="after")
    def _lease_matches_state(self) -> "AmbientPresenceStatePayload":
        if self.state == "present" and self.lease_ms == 0:
            raise ValueError("present ambient presence requires a lease")
        if self.state == "vacant" and self.lease_ms != 0:
            raise ValueError("vacant ambient presence cannot carry a lease")
        if self.state == "vacant" and self.observation == "heartbeat":
            raise ValueError("vacant ambient presence cannot be a heartbeat")
        return self


class OwnerPresenceConfirmedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ambient_source_device_id: str = Field(min_length=1, max_length=128)
    ambient_presence_epoch: int = Field(ge=1, le=4_294_967_295)
    profile_revision: int = Field(ge=1)
    guard_epoch: int = Field(ge=0, le=4_294_967_295)
    presence_sequence: int = Field(ge=1, le=4_294_967_295)
    evidence: Literal["local_owner_face"] = "local_owner_face"
    raw_retention: Literal["none"] = "none"
    lease_ms: int = Field(ge=1, le=120_000)


class OwnerPresenceChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: Literal["present", "absent"]
    profile_revision: int = Field(ge=1)
    guard_epoch: int = Field(ge=0, le=4_294_967_295)
    presence_sequence: int = Field(ge=1, le=4_294_967_295)
    lease_ms: int = Field(ge=0, le=120_000)
    evidence: Literal["face_gated_person_presence"] = "face_gated_person_presence"
    raw_retention: Literal["none"] = "none"

    @model_validator(mode="after")
    def _lease_matches_state(self) -> "OwnerPresenceChangedPayload":
        if self.state == "present" and self.lease_ms == 0:
            raise ValueError("present owner presence requires a lease")
        if self.state == "absent" and self.lease_ms != 0:
            raise ValueError("absent owner presence cannot carry a lease")
        return self


class CompanionFlowNodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    stage: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    status: Literal["running", "completed", "timeout", "failed"]
    label: str = Field(min_length=1, max_length=96)


class _DeviceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    schema_v: Literal[EVENT_SCHEMA_VERSION] = EVENT_SCHEMA_VERSION
    kind: Literal["event"] = "event"
    event_id: str = Field(min_length=1, max_length=96)
    flow_id: str = Field(min_length=1, max_length=96)
    causation_id: str = Field(default="", max_length=96)
    source: EventSource
    occurred_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=1)

    @model_validator(mode="after")
    def _ttl_is_short_and_forward(self) -> "_DeviceEvent":
        ttl_ms = self.expires_at_ms - self.occurred_at_ms
        if ttl_ms <= 0:
            raise ValueError("expires_at_ms must be after occurred_at_ms")
        if ttl_ms > EVENT_DEFAULT_TTL_MS:
            raise ValueError(f"device event TTL must not exceed {EVENT_DEFAULT_TTL_MS}ms")
        return self


class AmbientPresenceState(_DeviceEvent):
    type: Literal[AMBIENT_PRESENCE_STATE_TYPE] = AMBIENT_PRESENCE_STATE_TYPE
    payload: AmbientPresenceStatePayload

    @model_validator(mode="after")
    def _is_root_fact(self) -> "AmbientPresenceState":
        if self.causation_id:
            raise ValueError("ambient presence event must not have causation_id")
        return self


class IdentityOwnerPresenceConfirmed(_DeviceEvent):
    type: Literal[IDENTITY_OWNER_PRESENCE_CONFIRMED_TYPE] = IDENTITY_OWNER_PRESENCE_CONFIRMED_TYPE
    payload: OwnerPresenceConfirmedPayload

    @model_validator(mode="after")
    def _is_derived(self) -> "IdentityOwnerPresenceConfirmed":
        if not self.causation_id:
            raise ValueError("owner presence confirmation requires causation_id")
        if self.causation_id == self.event_id:
            raise ValueError("an event cannot cause itself")
        return self


class IdentityOwnerPresenceChanged(_DeviceEvent):
    type: Literal[IDENTITY_OWNER_PRESENCE_CHANGED_TYPE] = IDENTITY_OWNER_PRESENCE_CHANGED_TYPE
    payload: OwnerPresenceChangedPayload

    @model_validator(mode="after")
    def _is_root_fact(self) -> "IdentityOwnerPresenceChanged":
        if self.causation_id:
            raise ValueError("owner presence lifecycle event must not have causation_id")
        return self


class CompanionFlowNode(_DeviceEvent):
    type: Literal[COMPANION_FLOW_NODE_TYPE] = COMPANION_FLOW_NODE_TYPE
    payload: CompanionFlowNodePayload


DeviceEvent = Annotated[
    AmbientPresenceState
    | IdentityOwnerPresenceConfirmed
    | IdentityOwnerPresenceChanged
    | CompanionFlowNode,
    Field(discriminator="type"),
]

_DEVICE_EVENT_ADAPTER = TypeAdapter(DeviceEvent)


def _json_size(payload: object) -> int:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("device event must be JSON serializable") from exc
    return len(encoded)


def parse_device_event(payload: object, *, now_ms: int | None = None) -> DeviceEvent:
    if not isinstance(payload, Mapping):
        raise ValueError("device event must be an object")
    if _json_size(payload) > EVENT_MAX_BYTES:
        raise ValueError(f"device event must not exceed {EVENT_MAX_BYTES} bytes")
    event = _DEVICE_EVENT_ADAPTER.validate_python(payload)
    normalized = event.model_dump(mode="json")
    if _json_size(normalized) > EVENT_MAX_BYTES:
        raise ValueError(f"normalized device event must not exceed {EVENT_MAX_BYTES} bytes")
    if now_ms is not None:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")
        if event.expires_at_ms <= now_ms:
            raise ValueError("device event has expired")
        if event.occurred_at_ms > now_ms + EVENT_MAX_CLOCK_SKEW_MS:
            raise ValueError("device event occurred_at_ms is too far in the future")
    return event


def normalize_device_event(payload: object, *, now_ms: int | None = None) -> dict[str, object]:
    return parse_device_event(payload, now_ms=now_ms).model_dump(mode="json")
