"""Runtime normalization bindings over generated common contract shapes."""

from __future__ import annotations

from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

JsonObject: TypeAlias = dict[str, JsonValue]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DeviceIdentity(ContractModel):
    device_id: str = Field(min_length=1, max_length=128)
    public_key_fingerprint: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(default="local", min_length=1, max_length=64)
