"""Runtime normalization bindings for Provider channel-control contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel
from hub.contracts.bindings.device import DeviceManifest

ChannelKind = Literal["reliable-data", "realtime-data", "audio", "video"]


class ProviderChannelDevice(ContractModel):
    device_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=256)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest: DeviceManifest
    manifest_revision: str = Field(min_length=1, max_length=96)


class ProviderChannelProvisionRequest(ContractModel):
    operation: Literal["channel.provision-device"] = "channel.provision-device"
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


class ProviderChannelProvisionResponse(ContractModel):
    operation: Literal["channel.provisioned-device"] = "channel.provisioned-device"
    operation_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    manifest_revision: str = Field(min_length=1, max_length=96)
    channels: tuple[ChannelAssignment, ...] = Field(default=(), max_length=16)

    @field_validator("channels", mode="before")
    @classmethod
    def _channel_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ProviderChannelRevocationRequest(ContractModel):
    operation: Literal["channel.revoke-device"] = "channel.revoke-device"
    operation_id: str = Field(min_length=1, max_length=128)
    hub_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=256)


class ProviderChannelRevocationResponse(ContractModel):
    operation: Literal["channel.revoked-device"] = "channel.revoked-device"
    operation_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
