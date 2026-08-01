from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import dns.rdatatype
import httpx
import pytest

from hub.adapters.discovery.explicit_uri import ExplicitUriDiscovery
from hub.adapters.discovery.unicast_dns import UnicastDnsSdResolver
from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.ports.connections import ConnectorSupervisor


class _Ptr:
    target = "Hub._eidolon-hub._tcp.example.test."


class _Srv:
    target = "hub.example.test."
    port = 8443
    priority = 10
    weight = 20


class _Address:
    def __init__(self, address):
        self.address = address


class _Txt:
    strings = (
        b"descriptor_uri=https://hub.example.test:8443/api/connection/v1/descriptor",
        b"register_uri=https://hub.example.test:8443/api/connection/v1/register",
    )


class _Resolver:
    async def resolve(self, name, record_type):
        label = dns.rdatatype.to_text(record_type)
        if label == "PTR":
            return [_Ptr()]
        if label == "SRV":
            return [_Srv()]
        if label == "TXT":
            return [_Txt()]
        if label == "A":
            return [_Address("10.20.0.10")]
        if label == "AAAA":
            return [_Address("2001:db8::10")]
        raise AssertionError(label)


@pytest.mark.asyncio
async def test_unicast_dns_sd_resolves_ptr_srv_txt_a_and_aaaa() -> None:
    services = await UnicastDnsSdResolver(_Resolver()).resolve("_eidolon-hub._tcp.example.test")

    assert len(services) == 1
    assert services[0].target == "hub.example.test"
    assert services[0].addresses == ("10.20.0.10", "2001:db8::10")
    assert services[0].descriptor_uri.endswith("/descriptor")


@pytest.mark.asyncio
async def test_commissioned_explicit_uri_fetches_matching_https_descriptor() -> None:
    descriptor_uri = "https://hub.example.test/api/connection/v1/descriptor"

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "hub_id": "hub-example",
                "descriptor_uri": str(request.url),
                "https_registration_uri": ("https://hub.example.test/api/connection/v1/register"),
                "mqtt_endpoint_uri": None,
                "protocol_versions": [1],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        descriptors = await ExplicitUriDiscovery(client, (descriptor_uri,)).discover()

    assert descriptors[0].hub_id == "hub-example"


def test_explicit_uri_rejects_cleartext_commissioning_endpoint() -> None:
    with pytest.raises(ValueError, match="https"):
        ExplicitUriDiscovery(MagicMock(), ("http://hub.test/descriptor",))


@pytest.mark.asyncio
async def test_zeroconf_advertises_one_service_on_ipv4_and_ipv6_interfaces() -> None:
    with patch("hub.adapters.discovery.zeroconf.AsyncZeroconf") as constructor:
        instance = MagicMock()
        instance.async_register_service = AsyncMock()
        instance.async_unregister_service = AsyncMock()
        instance.async_close = AsyncMock()
        constructor.return_value = instance
        connector = ZeroconfHubAdvertiser(
            connector_id="mdns-local",
            service_type="_eidolon-hub._tcp.local.",
            service_name="Hub._eidolon-hub._tcp.local.",
            hostname="eidolon-hub",
            port=8443,
            descriptor_uri="https://eidolon-hub.local:8443/api/connection/v1/descriptor",
            registration_uri="https://eidolon-hub.local:8443/api/connection/v1/register",
            addresses=("192.168.10.5", "2001:db8::5"),
        )

        await connector.start()
        info = instance.async_register_service.await_args.args[0]
        await connector.stop()

    assert set(info.parsed_addresses()) == {"192.168.10.5", "2001:db8::5"}
    assert info.properties[b"descriptor_uri"].startswith(b"https://")
    assert b"config_url" not in info.properties


class _Connector:
    def __init__(self, connector_id, *, fail=False):
        self._connector_id = connector_id
        self.fail = fail
        self.started = 0
        self.stopped = 0

    @property
    def connector_id(self):
        return self._connector_id

    async def start(self):
        if self.fail:
            raise RuntimeError("start failed")
        self.started += 1

    async def stop(self):
        self.stopped += 1


@pytest.mark.asyncio
async def test_connector_supervisor_rolls_back_partial_start() -> None:
    first = _Connector("mdns")
    second = _Connector("mqtt", fail=True)

    with pytest.raises(RuntimeError, match="start failed"):
        await ConnectorSupervisor((first, second)).start()

    assert first.started == first.stopped == 1
