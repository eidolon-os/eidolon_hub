"""Runtime bindings for short-lived device enrollment and Provider handoff."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator

from hub.contracts.bindings.channel import ChannelAssignment
from hub.contracts.bindings.common import ContractModel, DeviceIdentity
from hub.contracts.bindings.device import DeviceLifecycleState, DeviceManifest


class HubDescriptor(ContractModel):
    schema_version: Literal[1] = 1
    hub_id: str = Field(min_length=1, max_length=128)
    descriptor_uri: str = Field(min_length=1, max_length=2048)
    device_onboarding_uri: str = Field(min_length=1, max_length=2048)
    enrollment_uri: str = Field(min_length=1, max_length=2048)
    protocol_versions: tuple[int, ...] = Field(default=(1,), min_length=1, max_length=8)

    @field_validator("protocol_versions", mode="before")
    @classmethod
    def _protocol_version_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("descriptor_uri", "device_onboarding_uri", "enrollment_uri")
    @classmethod
    def _https_uris(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Hub device-onboarding endpoints must use https")
        return value


class DeviceIdentityProof(ContractModel):
    algorithm: Literal["p256-sha256"] = "p256-sha256"
    public_key_spki: str = Field(min_length=80, max_length=256, repr=False)
    signature: str = Field(min_length=64, max_length=256, repr=False)


class PairingProofCommitment(ContractModel):
    method: Literal["local-secret-sha256"] = "local-secret-sha256"
    commitment: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", repr=False)


class DeviceEnrollment(ContractModel):
    operation: Literal["device.enrollment"] = "device.enrollment"
    request_id: str = Field(min_length=1, max_length=96)
    retrieval_token: str = Field(min_length=32, max_length=256, repr=False)
    identity: DeviceIdentity
    manifest: DeviceManifest
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(default="unknown", min_length=1, max_length=96)
    identity_proof: DeviceIdentityProof | None = Field(default=None, repr=False)
    pairing_proof: PairingProofCommitment | None = Field(default=None, repr=False)


class DeviceEnrollmentReceipt(ContractModel):
    operation: Literal["device.enrollment-received"] = "device.enrollment-received"
    request_id: str = Field(min_length=1, max_length=96)
    enrollment_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    lifecycle_state: DeviceLifecycleState
    retrieval_expires_at_ms: int = Field(ge=0)
    pairing_claim_uri: str | None = Field(default=None, max_length=2048)

    @field_validator("pairing_claim_uri")
    @classmethod
    def _https_pairing_claim_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("pairing claim endpoint must use https")
        return value


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
