"""Runtime normalization bindings for HTTPS device-access contracts."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel
from hub.contracts.bindings.device import DeviceRegistration


class HubDescriptor(ContractModel):
    schema_version: Literal[1] = 1
    hub_id: str = Field(min_length=1, max_length=128)
    descriptor_uri: str = Field(min_length=1, max_length=2048)
    device_access_uri: str = Field(min_length=1, max_length=2048)
    registration_uri: str = Field(min_length=1, max_length=2048)
    channel_acquisition_uri: str = Field(min_length=1, max_length=2048)
    protocol_versions: tuple[int, ...] = Field(default=(1,), min_length=1, max_length=8)

    @field_validator("protocol_versions", mode="before")
    @classmethod
    def _protocol_version_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator(
        "descriptor_uri",
        "device_access_uri",
        "registration_uri",
        "channel_acquisition_uri",
    )
    @classmethod
    def _https_uris(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Hub device-access endpoints must use https")
        return value


class SessionHello(ContractModel):
    operation: Literal["session.hello"] = "session.hello"
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    client_nonce: str = Field(min_length=16, max_length=256)


class SessionChallenge(ContractModel):
    operation: Literal["session.challenge"] = "session.challenge"
    request_id: str = Field(min_length=1, max_length=96)
    challenge_id: str = Field(min_length=1, max_length=96)
    server_nonce: str = Field(min_length=16, max_length=256)
    expires_at_ms: int = Field(ge=0)


class SessionProof(ContractModel):
    operation: Literal["session.proof"] = "session.proof"
    request_id: str = Field(min_length=1, max_length=96)
    challenge_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=32, max_length=4096)
    signature: str = Field(min_length=32, max_length=4096)


class SessionAccepted(ContractModel):
    operation: Literal["session.accepted"] = "session.accepted"
    request_id: str = Field(min_length=1, max_length=96)
    session_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    lease_expires_at_ms: int = Field(ge=0)
    heartbeat_after_ms: int = Field(ge=1_000, le=300_000)
    registration_required: bool = True


class SessionHeartbeat(ContractModel):
    operation: Literal["session.heartbeat"] = "session.heartbeat"
    request_id: str = Field(min_length=1, max_length=96)
    session_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    sequence: int = Field(ge=1)


class SessionClosed(ContractModel):
    operation: Literal["session.closed"] = "session.closed"
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    reason: Literal["client", "server", "lease-expired", "revoked"]


class SessionRegistration(ContractModel):
    operation: Literal["session.registration"] = "session.registration"
    session_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    registration: DeviceRegistration
