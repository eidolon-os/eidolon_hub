"""Runtime normalization bindings for connection wire contracts."""

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
    https_registration_uri: str = Field(min_length=1, max_length=2048)
    mqtt_endpoint_uri: str | None = Field(default=None, max_length=2048)
    protocol_versions: tuple[int, ...] = Field(default=(1,), min_length=1, max_length=8)

    @field_validator("protocol_versions", mode="before")
    @classmethod
    def _protocol_version_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("descriptor_uri", "https_registration_uri")
    @classmethod
    def _https_uris(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Hub descriptor endpoints must use https")
        return value

    @field_validator("mqtt_endpoint_uri")
    @classmethod
    def _mqtts_uri(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlparse(value)
            if parsed.scheme != "mqtts" or not parsed.netloc:
                raise ValueError("Hub MQTT endpoint must use mqtts")
        return value


class ConnectionHello(ContractModel):
    operation: Literal["connection.hello"] = "connection.hello"
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=96)
    client_nonce: str = Field(min_length=16, max_length=256)


class ConnectionChallenge(ContractModel):
    operation: Literal["connection.challenge"] = "connection.challenge"
    request_id: str = Field(min_length=1, max_length=96)
    challenge_id: str = Field(min_length=1, max_length=96)
    server_nonce: str = Field(min_length=16, max_length=256)
    expires_at_ms: int = Field(ge=0)


class ConnectionProof(ContractModel):
    operation: Literal["connection.proof"] = "connection.proof"
    request_id: str = Field(min_length=1, max_length=96)
    challenge_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=32, max_length=4096)
    signature: str = Field(min_length=32, max_length=4096)


class ConnectionAccepted(ContractModel):
    operation: Literal["connection.accepted"] = "connection.accepted"
    request_id: str = Field(min_length=1, max_length=96)
    connection_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    lease_expires_at_ms: int = Field(ge=0)
    heartbeat_after_ms: int = Field(ge=1_000, le=300_000)
    registration_required: bool = True


class ConnectionHeartbeat(ContractModel):
    operation: Literal["connection.heartbeat"] = "connection.heartbeat"
    request_id: str = Field(min_length=1, max_length=96)
    connection_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    sequence: int = Field(ge=1)


class ConnectionClosed(ContractModel):
    operation: Literal["connection.closed"] = "connection.closed"
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    connection_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    reason: Literal["client", "server", "lease-expired", "revoked"]


class ConnectionRegistration(ContractModel):
    operation: Literal["connection.registration"] = "connection.registration"
    connection_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=512, repr=False)
    registration: DeviceRegistration


class ChannelSignalDelivery(ContractModel):
    operation: Literal["channel.signal-delivery"] = "channel.signal-delivery"
    payload_json: str | None = Field(default=None, max_length=131_072, repr=False)


class ChannelSignalAccepted(ContractModel):
    operation: Literal["channel.signal-accepted"] = "channel.signal-accepted"
    request_id: str = Field(min_length=1, max_length=96)
    accepted: Literal[True] = True
