"""Strict privacy-preserving wire contracts for the sense plane."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

SENSE_SCHEMA_VERSION = 1
SENSE_ATTENTION_TYPE = "sense.attention"
SENSE_SESSION_TYPE = "sense.session"
SENSE_FATIGUE_TYPE = "sense.fatigue"
SENSE_EVENT_TYPE = "sense.event"
_SIGNAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


def _validate_signal_keys(
    value: dict[str, float | int | bool],
) -> dict[str, float | int | bool]:
    for key in value:
        if _SIGNAL_KEY_RE.fullmatch(key) is None:
            raise ValueError("sense signal names must be lowercase dotted identifiers")
    return value


class _SenseMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    schema_v: Literal[SENSE_SCHEMA_VERSION] = SENSE_SCHEMA_VERSION
    owner_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=96)
    epoch: int = Field(ge=0)
    ts_ms: int = Field(ge=0)


class SenseAttention(_SenseMessage):
    type: Literal[SENSE_ATTENTION_TYPE] = SENSE_ATTENTION_TYPE
    state: Literal["focused", "relaxed", "away_from_screen"]
    signals: dict[str, float | int | bool] = Field(default_factory=dict, max_length=16)
    raw_retention: Literal["none"] = "none"

    @field_validator("signals")
    @classmethod
    def _signals_are_bounded(
        cls,
        value: dict[str, float | int | bool],
    ) -> dict[str, float | int | bool]:
        return _validate_signal_keys(value)


class SenseSession(_SenseMessage):
    type: Literal[SENSE_SESSION_TYPE] = SENSE_SESSION_TYPE
    state: Literal["started", "ended"]
    duration_ms: int = Field(default=0, ge=0, le=86_400_000)
    interruptions: int = Field(default=0, ge=0, le=100_000)
    raw_retention: Literal["none"] = "none"

    @model_validator(mode="after")
    def _validate_session_shape(self) -> "SenseSession":
        if self.state == "started" and (self.duration_ms or self.interruptions):
            raise ValueError("started session must have duration_ms=0 and interruptions=0")
        return self


class SenseFatigue(_SenseMessage):
    type: Literal[SENSE_FATIGUE_TYPE] = SENSE_FATIGUE_TYPE
    hint: Literal["yawn", "drowsy", "slump"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    model_id: str = Field(min_length=1, max_length=96)
    model_version: str = Field(min_length=1, max_length=64)
    signals: dict[str, float | int | bool] = Field(default_factory=dict, max_length=16)
    raw_retention: Literal["none"] = "none"

    @field_validator("signals")
    @classmethod
    def _signals_are_bounded(
        cls,
        value: dict[str, float | int | bool],
    ) -> dict[str, float | int | bool]:
        return _validate_signal_keys(value)


class SenseEvent(_SenseMessage):
    type: Literal[SENSE_EVENT_TYPE] = SENSE_EVENT_TYPE
    event: Literal["person", "cat", "dog", "package"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    model_id: str = Field(min_length=1, max_length=96)
    model_version: str = Field(min_length=1, max_length=64)
    raw_retention: Literal["none"] = "none"


SenseMessage = Annotated[
    SenseAttention | SenseSession | SenseFatigue | SenseEvent,
    Field(discriminator="type"),
]
_SENSE_MESSAGE_ADAPTER = TypeAdapter(SenseMessage)


def parse_sense_message(payload: object) -> SenseMessage:
    return _SENSE_MESSAGE_ADAPTER.validate_python(payload)
