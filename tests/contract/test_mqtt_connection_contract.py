from __future__ import annotations

import json

import pytest

from hub.adapters.connections.mqtt import (
    MqttConnectionCodec,
    MqttContractRejected,
)
from hub.contracts.bindings.connection import ConnectionHello


def _topic(device_id: str = "device-1") -> str:
    return f"eidolon/v1/devices/{device_id}/connection/in"


def test_hello_is_accepted_only_in_device_connection_namespace() -> None:
    hello = ConnectionHello(
        request_id="request-1",
        device_id="device-1",
        connector_id="mqtt-cloud",
        client_nonce="0123456789abcdef",
    )

    decoded = MqttConnectionCodec.decode(_topic(), hello.model_dump_json().encode())

    assert decoded.device_id == "device-1"
    assert decoded.contract.operation == "connection.hello"


@pytest.mark.parametrize(
    "operation",
    ["device.command", "device.reported-state", "device.event", "channel.data", "audio.frame"],
)
def test_business_data_operations_are_rejected(operation: str) -> None:
    payload = json.dumps(
        {"operation": operation, "request_id": "request-1", "device_id": "device-1"}
    ).encode()

    with pytest.raises(MqttContractRejected, match="not allowed"):
        MqttConnectionCodec.decode(_topic(), payload)


def test_payload_device_cannot_escape_topic_acl() -> None:
    payload = (
        ConnectionHello(
            request_id="request-1",
            device_id="device-2",
            connector_id="mqtt-cloud",
            client_nonce="0123456789abcdef",
        )
        .model_dump_json()
        .encode()
    )

    with pytest.raises(MqttContractRejected, match="does not match"):
        MqttConnectionCodec.decode(_topic("device-1"), payload)


def test_non_connection_topic_is_rejected() -> None:
    with pytest.raises(MqttContractRejected, match="outside"):
        MqttConnectionCodec.decode(
            "eidolon/v1/devices/device-1/commands/in", b'{"operation":"connection.hello"}'
        )


def test_device_channel_negotiation_is_not_an_mqtt_inbound_operation() -> None:
    payload = json.dumps(
        {
            "operation": "channel.accept",
            "request_id": "request-1",
            "device_id": "device-1",
            "channel_id": "channel-1",
        }
    ).encode()

    with pytest.raises(MqttContractRejected, match="not allowed"):
        MqttConnectionCodec.decode(_topic(), payload)


def test_device_cannot_claim_server_or_revocation_disconnect_reason() -> None:
    payload = json.dumps(
        {
            "operation": "connection.closed",
            "request_id": "request-close",
            "device_id": "device-1",
            "connection_id": "connection-1",
            "lease_token": "signed-lease-token",
            "reason": "revoked",
        }
    ).encode()

    with pytest.raises(MqttContractRejected, match="client disconnect"):
        MqttConnectionCodec.decode(_topic(), payload)
