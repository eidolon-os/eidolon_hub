from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.channels.provisioner_client import ProvisionerClient
from hub.adapters.connections.mqtt import Mqtt5Connector
from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.domain.channels.entities import (
    ChannelKind,
    ChannelProfile,
    ChannelRequest,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _AsyncZeroconf:
    def __init__(self, *args, **kwargs):
        self.registered = []
        self.closed = False

    async def async_register_service(self, info, **kwargs):
        self.registered.append(info)

    async def async_unregister_service(self, info):
        self.registered.remove(info)

    async def async_close(self):
        self.closed = True


def _mdns_connector(monkeypatch):
    monkeypatch.setattr("hub.adapters.discovery.zeroconf.AsyncZeroconf", _AsyncZeroconf)
    return ZeroconfHubAdvertiser(
        connector_id="mdns-local",
        service_type="_eidolon-hub._tcp.local.",
        service_name="Hub._eidolon-hub._tcp.local.",
        hostname="eidolon-hub",
        port=8443,
        descriptor_uri="https://hub.local:8443/api/connection/v1/descriptor",
        registration_uri="https://hub.local:8443/api/connection/v1/register",
        addresses=("192.0.2.10",),
    )


def _mqtt_connector(monkeypatch):
    connector = Mqtt5Connector(
        connector_id="mqtt-cloud",
        hostname="broker.example.test",
        port=8883,
        handler=lambda _message: None,
    )

    async def remain_started():
        await asyncio.Event().wait()

    monkeypatch.setattr(connector, "_run", remain_started)
    return connector


@pytest.mark.parametrize("factory", (_mdns_connector, _mqtt_connector), ids=("mdns", "mqtt5"))
async def test_connection_connectors_share_idempotent_lifecycle_contract(
    factory, monkeypatch
) -> None:
    connector = factory(monkeypatch)

    assert connector.connector_id
    await connector.start()
    await connector.start()
    await connector.stop()
    await connector.stop()


class _RequestReply:
    def __init__(self):
        self.operations = []

    async def request(self, subject, payload, *, timeout):
        message = json.loads(payload)
        self.operations.append((subject, message["operation"], timeout))
        if message["operation"] == "channel.revoke":
            return b"{}"
        return json.dumps(
            {
                "request_id": message.get("request_id", "renew-request"),
                "channel_id": message.get("channel_id", "channel-1"),
                "device_id": message["device_id"],
                "profile_name": message["profile_name"],
                "issued_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                "renew_after": (NOW + timedelta(minutes=4)).isoformat(),
                "opaque_binding_b64": base64.b64encode(b"provider-owned").decode(),
            }
        ).encode()


async def test_external_channel_provider_client_conforms_to_full_lifecycle_port() -> None:
    transport = _RequestReply()
    provisioner = ProvisionerClient(
        transport,
        route="https://provider.example/v1/channels/operations",
        timeout_seconds=3,
    )
    request = ChannelRequest(
        request_id="request-1",
        device_id="device-1",
        profile=ChannelProfile(
            name="management-data",
            required_kinds=frozenset({ChannelKind.RELIABLE_DATA}),
            provisioner_ref="provider/default",
        ),
        requested_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    grant = await provisioner.provision(request)
    renewed = await provisioner.renew(grant.lease)
    await provisioner.revoke(renewed.lease, reason="test-complete")

    assert [item[1] for item in transport.operations] == [
        "channel.provision",
        "channel.renew",
        "channel.revoke",
    ]
    assert "provider-owned" not in repr(grant)
