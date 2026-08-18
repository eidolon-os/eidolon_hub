"""Runtime bindings for short-lived device enrollment and Provider handoff."""

from __future__ import annotations

from typing import Literal
from eidolon_sdk.device_foundation.v1 import OwnerDomainDescriptor
from pydantic import Field, field_validator

from hub.contracts.bindings.channel import ChannelAssignment
from hub.contracts.bindings.common import ContractModel, DeviceIdentity
from hub.contracts.bindings.device import DeviceLifecycleState, DeviceManifest

__all__ = [
    "DeviceEnrollment",
    "DeviceEnrollmentReceipt",
    "DeviceHandoffOutcome",
    "DeviceHandoffRequest",
    "OwnerDomainDescriptor",
]


class DeviceEnrollment(ContractModel):
    operation: Literal["device.enrollment"] = "device.enrollment"
    request_id: str = Field(min_length=1, max_length=96)
    retrieval_token: str = Field(min_length=32, max_length=256, repr=False)
    identity: DeviceIdentity
    manifest: DeviceManifest
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(default="unknown", min_length=1, max_length=96)


class DeviceEnrollmentReceipt(ContractModel):
    operation: Literal["device.enrollment-received"] = "device.enrollment-received"
    request_id: str = Field(min_length=1, max_length=96)
    enrollment_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    lifecycle_state: DeviceLifecycleState
    retrieval_expires_at_ms: int = Field(ge=0)


class DeviceHandoffRequest(ContractModel):
    operation: Literal["device.handoff"] = "device.handoff"
    request_id: str = Field(min_length=1, max_length=96)
    retrieval_token: str = Field(min_length=32, max_length=256, repr=False)


class DeviceHandoffOutcome(ContractModel):
    operation: Literal["device.handoff-outcome"] = "device.handoff-outcome"
    request_id: str = Field(min_length=1, max_length=96)
    enrollment_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    manifest_revision: str = Field(min_length=1, max_length=96)
    lifecycle_state: DeviceLifecycleState
    channels: tuple[ChannelAssignment, ...] = Field(default=(), max_length=16)

    @field_validator("channels", mode="before")
    @classmethod
    def _channel_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value
