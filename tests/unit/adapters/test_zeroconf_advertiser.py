from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.composition.device_onboarding import _hub_descriptor, _mdns_advertiser
from hub.config import HubConfig, OnboardingConfig


async def test_zeroconf_advertises_the_https_descriptor_on_all_addresses() -> None:
    with patch("hub.adapters.discovery.zeroconf.AsyncZeroconf") as constructor:
        instance = MagicMock()
        instance.async_register_service = AsyncMock()
        instance.async_unregister_service = AsyncMock()
        instance.async_close = AsyncMock()
        constructor.return_value = instance
        advertiser = ZeroconfHubAdvertiser(
            advertisement_id="mdns-local",
            service_type="_eidolon-hub._tcp.local.",
            service_name="Hub._eidolon-hub._tcp.local.",
            hostname="eidolon-hub",
            port=8443,
            descriptor_uri="https://eidolon-hub.local:8443/api/device-onboarding/v1/descriptor",
            enrollment_uri="https://eidolon-hub.local:8443/api/device-onboarding/v1/enrollments",
            addresses=("192.168.10.5", "2001:db8::5"),
        )

        await advertiser.start()
        info = instance.async_register_service.await_args.args[0]
        await advertiser.stop()

    assert set(info.parsed_addresses()) == {"192.168.10.5", "2001:db8::5"}
    assert info.properties[b"descriptor_uri"].startswith(b"https://")
    assert info.properties[b"enrollment_uri"].endswith(b"/enrollments")
    assert b"config_url" not in info.properties


async def test_zeroconf_start_is_idempotent_and_stop_releases_runtime() -> None:
    with patch("hub.adapters.discovery.zeroconf.AsyncZeroconf") as constructor:
        instance = MagicMock()
        instance.async_register_service = AsyncMock()
        instance.async_unregister_service = AsyncMock()
        instance.async_close = AsyncMock()
        constructor.return_value = instance
        advertiser = ZeroconfHubAdvertiser(
            advertisement_id="mdns-local",
            service_type="_eidolon-hub._tcp.local.",
            service_name="Hub._eidolon-hub._tcp.local.",
            hostname="eidolon-hub",
            port=8443,
            descriptor_uri="https://eidolon-hub.local:8443/api/device-onboarding/v1/descriptor",
            enrollment_uri="https://eidolon-hub.local:8443/api/device-onboarding/v1/enrollments",
            addresses=("192.168.10.5",),
        )

        await advertiser.start()
        await advertiser.start()
        await advertiser.stop()
        await advertiser.stop()

    instance.async_register_service.assert_awaited_once()
    instance.async_close.assert_awaited_once()


def test_mdns_identity_and_port_derive_from_public_contract() -> None:
    config = HubConfig(
        onboarding=OnboardingConfig(
            hub_id="living-room-hub",
            public_base_url="https://living-room.local:8443",
        )
    )

    advertiser = _mdns_advertiser(config, _hub_descriptor(config))

    assert advertiser is not None
    assert advertiser._service_type == "_eidolon-hub._tcp.local."
    assert advertiser._service_name == "living-room-hub._eidolon-hub._tcp.local."
    assert advertiser._hostname == "living-room"
    assert advertiser._port == 8443


def test_mdns_can_advertise_a_public_ca_tls_uri_over_the_local_link() -> None:
    config = HubConfig(
        onboarding=OnboardingConfig(
            hub_id="Living Room Hub",
            public_base_url="https://hub.example.com:8443",
        )
    )

    advertiser = _mdns_advertiser(config, _hub_descriptor(config))

    assert advertiser is not None
    assert advertiser._hostname == "living-room-hub"
    assert advertiser._port == 8443
    assert advertiser._descriptor_uri.startswith("https://hub.example.com:8443/")


def test_a_link_local_address_is_never_advertised() -> None:
    """169.254/16 describes one cable, not a network a phone can be sent to.

    The IPv6 half of this was filtered from the start; the IPv4 half was not.
    A Host with a second NIC — an operator's direct wire, or simply Ethernet
    beside Wi-Fi — therefore advertised its link-local address, and because
    addresses sort as text that one even preceded the routable one. Every
    device on the LAN was told to reach the Hub somewhere it cannot route to.
    """

    from hub.adapters.discovery import zeroconf as module

    class _Adapter:
        def __init__(self, ips):
            self.ips = ips

    class _IP:
        def __init__(self, ip):
            self.ip = ip

    adapters = [
        _Adapter([_IP("169.254.55.2"), _IP("192.168.100.15")]),
        _Adapter([_IP(("fe80::1", 0, 0))]),
        _Adapter([_IP("127.0.0.1")]),
    ]
    with patch.object(module.ifaddr, "get_adapters", return_value=adapters):
        assert module.interface_addresses() == ("192.168.100.15",)


def test_a_routable_address_is_still_advertised_when_it_is_the_only_one() -> None:
    from hub.adapters.discovery import zeroconf as module

    class _Adapter:
        def __init__(self, ips):
            self.ips = ips

    class _IP:
        def __init__(self, ip):
            self.ip = ip

    with patch.object(
        module.ifaddr, "get_adapters", return_value=[_Adapter([_IP("10.0.0.4")])]
    ):
        assert module.interface_addresses() == ("10.0.0.4",)
