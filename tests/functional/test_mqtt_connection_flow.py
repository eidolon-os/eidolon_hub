from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hub.adapters.connections.mqtt import (
    MqttConnectionCodec,
    MqttContractRejected,
    create_mqtt_application_handler,
)

TOPIC = "eidolon/v1/devices/device-1/connection/in"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Enroll:
    async def begin(self, hello):
        self.hello = hello
        return SimpleNamespace(
            challenge_id="challenge-1",
            server_nonce="server-nonce-0123456789",
            expires_at=NOW + timedelta(seconds=30),
        )

    async def complete(self, **kwargs):
        self.proof = kwargs
        return _lease(), True


class _Register:
    async def execute(self, **kwargs):
        self.arguments = kwargs


class _Renew:
    async def execute(self, **kwargs):
        self.arguments = kwargs
        return _lease()


class _Close:
    async def execute(self, **kwargs):
        self.arguments = kwargs


def _lease():
    return SimpleNamespace(
        connection_id="connection-1",
        lease_token="signed-lease-token-device-1",
        expires_at=NOW + timedelta(seconds=45),
    )


def _message(payload: dict):
    return MqttConnectionCodec.decode(TOPIC, json.dumps(payload).encode())


async def test_mqtt_lifecycle_executes_shared_application_contracts() -> None:
    enroll = _Enroll()
    register = _Register()
    renew = _Renew()
    close = _Close()
    handler = create_mqtt_application_handler(
        enroll=enroll,
        register=register,
        renew=renew,
        close=close,
        expected_connector_id="mqtt-cloud",
    )

    challenge = await handler(
        _message(
            {
                "operation": "connection.hello",
                "request_id": "request-hello",
                "device_id": "device-1",
                "connector_id": "mqtt-cloud",
                "client_nonce": "0123456789abcdef",
            }
        )
    )
    assert json.loads(challenge)["operation"] == "connection.challenge"

    accepted = await handler(
        _message(
            {
                "operation": "connection.proof",
                "request_id": "request-proof",
                "challenge_id": "challenge-1",
                "device_id": "device-1",
                "public_key": "p" * 32,
                "signature": "s" * 32,
            }
        )
    )
    assert json.loads(accepted)["connection_id"] == "connection-1"

    await handler(
        _message(
            {
                "operation": "connection.registration",
                "connection_id": "connection-1",
                "lease_token": "signed-lease-token-device-1",
                "registration": {
                    "operation": "device.registration",
                    "request_id": "request-register",
                    "identity": {
                        "device_id": "device-1",
                        "public_key_fingerprint": "p256:fingerprint",
                        "tenant_id": "cloud",
                    },
                    "manifest": {
                        "schema_version": 1,
                        "title": "Generic Device",
                        "properties": [],
                        "actions": [],
                        "events": [],
                        "media": [],
                    },
                    "display_name": "Generic Device",
                    "device_kind": "generic",
                },
            }
        )
    )
    assert register.arguments["registration"].identity.device_id == "device-1"

    heartbeat = await handler(
        _message(
            {
                "operation": "connection.heartbeat",
                "request_id": "request-heartbeat",
                "connection_id": "connection-1",
                "lease_token": "signed-lease-token-device-1",
                "sequence": 7,
            }
        )
    )
    assert renew.arguments["sequence"] == 7
    assert json.loads(heartbeat)["registration_required"] is False

    await handler(
        _message(
            {
                "operation": "connection.closed",
                "request_id": "request-close",
                "device_id": "device-1",
                "connection_id": "connection-1",
                "lease_token": "signed-lease-token-device-1",
                "reason": "client",
            }
        )
    )
    assert close.arguments["lease_token"] == "signed-lease-token-device-1"


async def test_mqtt_binding_rejects_a_spoofed_connector_id() -> None:
    handler = create_mqtt_application_handler(
        enroll=_Enroll(),
        register=_Register(),
        renew=_Renew(),
        close=_Close(),
        expected_connector_id="mqtt-cloud",
    )

    with pytest.raises(MqttContractRejected, match="does not match"):
        await handler(
            _message(
                {
                    "operation": "connection.hello",
                    "request_id": "request-hello",
                    "device_id": "device-1",
                    "connector_id": "mdns-local",
                    "client_nonce": "0123456789abcdef",
                }
            )
        )
