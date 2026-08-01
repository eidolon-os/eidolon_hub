from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.channels.provider_client import ChannelProviderHttpClient
from hub.adapters.connections.mqtt import Mqtt5Connector
from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.domain.channels.entities import ProviderDeviceContext

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _AsyncZeroconf:
    def __init__(self, *args, **kwargs):
        self.registered = []

    async def async_register_service(self, info, **kwargs):
        self.registered.append(info)

    async def async_unregister_service(self, info):
        self.registered.remove(info)

    async def async_close(self):
        pass


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


class _ProviderTransport:
    async def request(self, route, payload, *, timeout):
        request = json.loads(payload)
        return json.dumps(
            {
                "operation": "channel.assignments",
                "operation_id": request["operation_id"],
                "device_id": request["device"]["device_id"],
                "manifest_revision": request["device"]["manifest_revision"],
                "channels": [
                    {
                        "channel_id": "channel-1",
                        "purpose": "management",
                        "kinds": ["reliable-data"],
                        "binding_format": "application/test+json",
                        "issued_at_ms": int(NOW.timestamp() * 1000),
                        "expires_at_ms": int((NOW + timedelta(minutes=5)).timestamp() * 1000),
                        "opaque_binding": base64.b64encode(b"provider-owned").decode(),
                    }
                ],
            }
        ).encode()


async def test_channel_provider_adapter_conforms_to_single_desired_state_port() -> None:
    client = ChannelProviderHttpClient(
        _ProviderTransport(), contract_url="https://provider.example/v1"
    )
    context = ProviderDeviceContext(
        operation_id="channel-sync:sha256:desired",
        hub_id="hub-1",
        device_id="device-1",
        public_key_fingerprint="p256:fingerprint",
        tenant_id="default",
        owner_id="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json='{"schema_version":1,"title":"Device"}',
        manifest_revision="sha256:manifest",
        approved=True,
        revoked=False,
        connected=True,
    )

    assignments = await client.sync_device(context)

    assert assignments.device_id == "device-1"
    assert assignments.grants[0].opaque_binding.relay_bytes() == b"provider-owned"
