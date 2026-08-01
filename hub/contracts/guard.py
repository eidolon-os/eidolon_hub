"""Strict Guard wire contracts owned by Hub.

These models deliberately exclude raw media, biometric templates and arbitrary
metadata.  Guard is a device-management contract; capture implementation stays
on the device side.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

GUARD_SCHEMA_VERSION = 1
GUARD_RUNTIME_SCHEMA_VERSION = 1
SILENT_PRESENCE_POLICY_ID = "silent_presence"
GUARD_PRESENCE_CANDIDATE_TYPE = "guard.presence.candidate"
GUARD_PRESENCE_ABSENT_TYPE = "guard.presence.absent"
GUARD_OWNER_PRESENCE_TYPE = "guard.owner_presence"
_SIGNAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")

OWNER_FACE_PROFILE_SCHEMA_VERSION = 1
OWNER_FACE_PROFILE_MIN_REFERENCES = 3
OWNER_FACE_PROFILE_MAX_REFERENCES = 5
OWNER_FACE_PROFILE_REQUIRED_POSES = frozenset({"front", "left", "right"})
OwnerFacePose = Literal["front", "left", "right", "down", "up"]
OwnerFaceDesiredState = Literal["active", "cleared"]


class GuardSilentPresenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_v: Literal[GUARD_SCHEMA_VERSION] = GUARD_SCHEMA_VERSION
    candidate_enabled: bool = True
    verified_enabled: bool = True
    accepted_verdicts: tuple[Literal["present", "unknown"], ...] = Field(
        default=("present", "unknown"), min_length=1
    )
    absence_enabled: bool = True

    @field_validator("accepted_verdicts", mode="before")
    @classmethod
    def _arrays_to_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("accepted_verdicts")
    @classmethod
    def _unique_verdicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("accepted_verdicts must not contain duplicates")
        return value


class GuardRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_v: Literal[GUARD_RUNTIME_SCHEMA_VERSION] = GUARD_RUNTIME_SCHEMA_VERSION
    sample_interval_ms: int = Field(default=200, ge=200, le=60_000)
    preview_interval_ms: int = Field(default=1_000, ge=200, le=60_000)
    motion_threshold: int = Field(default=18, ge=0, le=255)
    motion_clear_threshold: int = Field(default=9, ge=0, le=255)
    candidate_debounce_ms: int = Field(default=1_000, ge=200, le=600_000)
    absence_timeout_ms: int = Field(default=180_000, ge=400, le=3_600_000)
    consecutive_capture_failures: int = Field(default=5, ge=1, le=100)
    owner_face_interval_ms: int = Field(default=500, ge=500, le=60_000)
    owner_presence_enter_ms: int = Field(default=600, ge=500, le=60_000)
    owner_presence_exit_ms: int = Field(default=12_000, ge=1_000, le=600_000)
    owner_presence_heartbeat_ms: int = Field(default=10_000, ge=1_000, le=300_000)
    owner_presence_lease_ms: int = Field(default=30_000, ge=5_000, le=600_000)

    @model_validator(mode="after")
    def _relations(self) -> "GuardRuntimeConfig":
        if self.preview_interval_ms < self.sample_interval_ms:
            raise ValueError("preview_interval_ms must be at least sample_interval_ms")
        if self.motion_clear_threshold > self.motion_threshold:
            raise ValueError("motion_clear_threshold must not exceed motion_threshold")
        if self.candidate_debounce_ms < self.sample_interval_ms:
            raise ValueError("candidate_debounce_ms must be at least sample_interval_ms")
        if self.absence_timeout_ms < self.sample_interval_ms * 2:
            raise ValueError("absence_timeout_ms must be at least twice sample_interval_ms")
        if self.owner_presence_enter_ms < self.owner_face_interval_ms:
            raise ValueError("owner_presence_enter_ms must be at least owner_face_interval_ms")
        if self.owner_presence_exit_ms < self.owner_face_interval_ms * 2:
            raise ValueError("owner_presence_exit_ms must be at least twice owner_face_interval_ms")
        if self.owner_presence_heartbeat_ms >= self.owner_presence_lease_ms:
            raise ValueError(
                "owner_presence_heartbeat_ms must be less than owner_presence_lease_ms"
            )
        return self


class _GuardMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    schema_v: Literal[GUARD_SCHEMA_VERSION] = GUARD_SCHEMA_VERSION
    guard_companion_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=96)
    guard_epoch: int = Field(ge=0)
    ts_ms: int = Field(ge=0)


class GuardCameraFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    frame_hash: str | None = Field(default=None, min_length=8, max_length=128)
    motion_score: float | None = Field(default=None, ge=0)


class GuardPresenceCandidate(_GuardMessage):
    type: Literal[GUARD_PRESENCE_CANDIDATE_TYPE] = GUARD_PRESENCE_CANDIDATE_TYPE
    signals: dict[str, float | int | bool] = Field(default_factory=dict, max_length=16)
    camera: GuardCameraFact | None = None
    raw_retention: Literal["none"] = "none"
    debounce_ms: int = Field(default=0, ge=0, le=600_000)

    @field_validator("signals")
    @classmethod
    def _bounded_signal_names(cls, value: dict[str, float | int | bool]):
        if any(_SIGNAL_KEY_RE.fullmatch(key) is None for key in value):
            raise ValueError("guard signal names must be lowercase dotted identifiers")
        return value


class GuardPresenceVerified(_GuardMessage):
    type: Literal["guard.presence.verified"] = "guard.presence.verified"
    verifier: Literal["fixture"] = "fixture"
    verdict: Literal["present", "unknown", "rejected"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    raw_retention: Literal["none"] = "none"


class GuardPresenceAbsent(_GuardMessage):
    type: Literal[GUARD_PRESENCE_ABSENT_TYPE] = GUARD_PRESENCE_ABSENT_TYPE
    reason: Literal["timeout", "clear", "policy"]
    absent_for_ms: int = Field(ge=0)
    raw_retention: Literal["none"] = "none"


class GuardOwnerPresence(_GuardMessage):
    type: Literal[GUARD_OWNER_PRESENCE_TYPE] = GUARD_OWNER_PRESENCE_TYPE
    state: Literal["present", "absent"]
    profile_revision: int = Field(ge=1)
    sequence: int = Field(ge=1)
    lease_ms: int = Field(ge=0, le=600_000)
    raw_retention: Literal["none"] = "none"

    @model_validator(mode="after")
    def _lease_matches_state(self) -> "GuardOwnerPresence":
        if self.state == "present" and self.lease_ms < 5_000:
            raise ValueError("present owner presence requires a lease of at least 5000ms")
        if self.state == "absent" and self.lease_ms != 0:
            raise ValueError("absent owner presence must have lease_ms=0")
        return self


class GuardPolicyAction(_GuardMessage):
    type: Literal["guard.policy.action"] = "guard.policy.action"
    action_id: str = Field(min_length=1, max_length=96)
    policy_id: str = Field(min_length=1, max_length=64)
    action: Literal["mission_control.annotate", "mission_control.clear", "body.presence.set"]
    subscriber: str = Field(default="mission_control_fixture", min_length=1, max_length=128)
    payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    raw_retention: Literal["none"] = "none"


class GuardPolicyActionAck(_GuardMessage):
    type: Literal["guard.policy.action_ack"] = "guard.policy.action_ack"
    action_id: str = Field(min_length=1, max_length=96)
    subscriber: str = Field(default="mission_control_fixture", min_length=1, max_length=128)
    status: Literal["accepted", "completed", "failed"]
    message: str = Field(default="", max_length=256)


GuardMessage = Annotated[
    GuardPresenceCandidate
    | GuardPresenceVerified
    | GuardPresenceAbsent
    | GuardOwnerPresence
    | GuardPolicyAction
    | GuardPolicyActionAck,
    Field(discriminator="type"),
]
_GUARD_ADAPTER = TypeAdapter(GuardMessage)


class GuardOwnerFaceReferenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reference_id: str = Field(min_length=1, max_length=96)
    pose: OwnerFacePose
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=4 * 1024 * 1024)
    content_type: Literal["image/jpeg"] = "image/jpeg"


class GuardOwnerFaceProfileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_v: Literal[OWNER_FACE_PROFILE_SCHEMA_VERSION] = OWNER_FACE_PROFILE_SCHEMA_VERSION
    binding_id: str = Field(min_length=1, max_length=64)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_revision: int = Field(ge=1)
    desired_state: OwnerFaceDesiredState
    model_id: str | None = Field(default=None, min_length=1, max_length=96)
    preprocessing_version: str | None = Field(default=None, min_length=1, max_length=96)
    references: tuple[GuardOwnerFaceReferenceManifest, ...] = Field(
        default=(), max_length=OWNER_FACE_PROFILE_MAX_REFERENCES
    )

    @field_validator("references", mode="before")
    @classmethod
    def _reference_arrays_to_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _state_is_complete(self) -> "GuardOwnerFaceProfileManifest":
        if self.desired_state == "cleared":
            if (
                self.references
                or self.model_id is not None
                or self.preprocessing_version is not None
            ):
                raise ValueError("cleared owner face profile must not contain model or references")
            return self
        if self.model_id is None or self.preprocessing_version is None:
            raise ValueError("active owner face profile requires model compatibility metadata")
        if len(self.references) < OWNER_FACE_PROFILE_MIN_REFERENCES:
            raise ValueError("active owner face profile requires at least three references")
        ids = [reference.reference_id for reference in self.references]
        poses = [reference.pose for reference in self.references]
        if len(ids) != len(set(ids)):
            raise ValueError("owner face reference ids must be unique")
        if len(poses) != len(set(poses)):
            raise ValueError("owner face reference poses must be unique")
        if not OWNER_FACE_PROFILE_REQUIRED_POSES.issubset(poses):
            raise ValueError("owner face profile requires front, left, and right references")
        return self


class GuardOwnerFaceProfileSync(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    binding_id: str = Field(min_length=1, max_length=64)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_revision: int = Field(ge=1)
    desired_state: OwnerFaceDesiredState


class GuardOwnerFaceProfileApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    binding_id: str = Field(min_length=1, max_length=64)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_revision: int = Field(ge=1)
    applied_state: OwnerFaceDesiredState
    model_id: str | None = Field(default=None, min_length=1, max_length=96)
    preprocessing_version: str | None = Field(default=None, min_length=1, max_length=96)
    template_count: int = Field(ge=0, le=OWNER_FACE_PROFILE_MAX_REFERENCES)

    @model_validator(mode="after")
    def _state_is_complete(self) -> "GuardOwnerFaceProfileApplyResult":
        if self.applied_state == "cleared":
            if (
                self.template_count
                or self.model_id is not None
                or self.preprocessing_version is not None
            ):
                raise ValueError("cleared owner face result must not retain model or templates")
            return self
        if self.model_id is None or self.preprocessing_version is None:
            raise ValueError("active owner face result requires model compatibility metadata")
        if self.template_count < OWNER_FACE_PROFILE_MIN_REFERENCES:
            raise ValueError("active owner face result requires at least three templates")
        return self


def parse_guard_message(payload: object) -> GuardMessage:
    return _GUARD_ADAPTER.validate_python(payload)


def parse_guard_policy_config(policy_id: str, payload: object | None) -> GuardSilentPresenceConfig:
    if policy_id != SILENT_PRESENCE_POLICY_ID:
        raise ValueError(f"unsupported guard policy: {policy_id!r}")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("guard policy config must be an object")
    return GuardSilentPresenceConfig.model_validate(payload)


def normalize_guard_policy_config(policy_id: str, payload: object | None) -> dict[str, object]:
    return parse_guard_policy_config(policy_id, payload).model_dump(mode="json")


def parse_guard_runtime_config(payload: object | None) -> GuardRuntimeConfig:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("guard runtime config must be an object")
    return GuardRuntimeConfig.model_validate(payload)


def normalize_guard_runtime_config(payload: object | None) -> dict[str, object]:
    return parse_guard_runtime_config(payload).model_dump(mode="json")


def parse_guard_owner_face_profile_manifest(payload: object) -> GuardOwnerFaceProfileManifest:
    return GuardOwnerFaceProfileManifest.model_validate(payload)


def parse_guard_owner_face_profile_sync(payload: object) -> GuardOwnerFaceProfileSync:
    return GuardOwnerFaceProfileSync.model_validate(payload)


def parse_guard_owner_face_profile_apply_result(
    payload: object,
) -> GuardOwnerFaceProfileApplyResult:
    return GuardOwnerFaceProfileApplyResult.model_validate(payload)
