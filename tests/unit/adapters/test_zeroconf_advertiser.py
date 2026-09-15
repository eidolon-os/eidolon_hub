from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from hub.adapters.discovery.zeroconf import ZeroconfAuthorityCandidateAdvertiser
from hub.composition.device_onboarding import _mdns_advertiser
from hub.config import HubConfig, OnboardingConfig


async def test_zeroconf_advertises_the_https_descriptor_on_all_addresses() -> None:
    with patch("hub.adapters.discovery.zeroconf.AsyncZeroconf") as constructor:
        instance = MagicMock()
        instance.async_register_service = AsyncMock()
        instance.async_unregister_service = AsyncMock()
        instance.async_close = AsyncMock()
        constructor.return_value = instance
        advertiser = ZeroconfAuthorityCandidateAdvertiser(
            advertisement_id="mdns-local",
            service_type="_eidolon-owner._tcp.local.",
            service_name="owner-local._eidolon-owner._tcp.local.",
            hostname="eidolon-hub",
            port=8443,
            owner_domain_id="owner-local",
            owner_domain_descriptor_uri="https://eidolon-hub.local:8443/api/device-onboarding/v1/descriptor",
            addresses=("192.168.10.5", "2001:db8::5"),
        )

        await advertiser.start()
        info = instance.async_register_service.await_args.args[0]
        await advertiser.stop()

    assert set(info.parsed_addresses()) == {"192.168.10.5", "2001:db8::5"}
    assert info.properties[b"owner_domain_id"] == b"owner-local"
    assert info.properties[b"owner_domain_descriptor_uri"].startswith(b"https://")
    assert b"config_url" not in info.properties


async def test_zeroconf_start_is_idempotent_and_stop_releases_runtime() -> None:
    with patch("hub.adapters.discovery.zeroconf.AsyncZeroconf") as constructor:
        instance = MagicMock()
        instance.async_register_service = AsyncMock()
        instance.async_unregister_service = AsyncMock()
        instance.async_close = AsyncMock()
        constructor.return_value = instance
        advertiser = ZeroconfAuthorityCandidateAdvertiser(
            advertisement_id="mdns-local",
            service_type="_eidolon-owner._tcp.local.",
            service_name="owner-local._eidolon-owner._tcp.local.",
            hostname="eidolon-hub",
            port=8443,
            owner_domain_id="owner-local",
            owner_domain_descriptor_uri="https://eidolon-hub.local:8443/api/device-onboarding/v1/descriptor",
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
            owner_domain_id="living-room-owner",
            descriptor_uri="https://living-room.local:8443/api/device-onboarding/v1/descriptor",
        )
    )

    advertiser = _mdns_advertiser(config)

    assert advertiser is not None
    assert advertiser._service_type == "_eidolon-owner._tcp.local."
    assert advertiser._service_name == "living-room-owner._eidolon-owner._tcp.local."
    assert advertiser._hostname == "living-room"
    assert advertiser._port == 8443


def test_mdns_can_advertise_a_public_ca_tls_uri_over_the_local_link() -> None:
    config = HubConfig(
        onboarding=OnboardingConfig(
            owner_domain_id="Living Room Owner",
            descriptor_uri="https://hub.example.com:8443/api/device-onboarding/v1/descriptor",
        )
    )

    advertiser = _mdns_advertiser(config)

    assert advertiser is not None
    assert advertiser._hostname == "living-room-owner"
    assert advertiser._port == 8443
    assert advertiser._owner_domain_descriptor_uri.startswith(
        "https://hub.example.com:8443/"
    )


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


async def test_network_transport_replaced_on_address_or_interface_change() -> None:
    from hub.adapters.discovery import zeroconf as m
    instances = []
    def construct(**_kwargs):
        instance = MagicMock()
        for method in ("async_register_service", "async_unregister_service", "async_close"):
            setattr(instance, method, AsyncMock())
        instances.append(instance)
        return instance
    with patch.object(m, "AsyncZeroconf", side_effect=construct), patch.object(
        m, "interface_snapshot", return_value=(("wlan0", 3, "192.168.1.37", 24),)
    ) as snapshot:
        a = _dynamic_advertiser()
        await a.start()
        await a.refresh_interfaces()
        assert len(instances) == 1
        snapshot.return_value = (("wlan0", 3, "10.183.24.39", 24),)
        await a.refresh_interfaces()
        assert len(instances) == 2
        instances[0].async_close.assert_awaited_once()
        assert a._info.parsed_addresses() == ["10.183.24.39"]
        snapshot.return_value = (("wlan1", 4, "10.183.24.39", 24),)
        await a.refresh_interfaces()
        assert len(instances) == 3
        await a.stop()


def _dynamic_advertiser():
    return ZeroconfAuthorityCandidateAdvertiser(
        advertisement_id="test", service_type="_eidolon-owner._tcp.local.",
        service_name="test._eidolon-owner._tcp.local.", hostname="test",
        port=9443, owner_domain_id="test",
        owner_domain_descriptor_uri="https://test.local:9443/descriptor", refresh_seconds=0,
    )


async def test_no_network_and_registration_failure_recover_without_hub_restart() -> None:
    from hub.adapters.discovery import zeroconf as m
    with patch.object(m, "AsyncZeroconf") as constructor, patch.object(
        m, "interface_snapshot", return_value=()
    ) as snapshot:
        broken, healthy = MagicMock(), MagicMock()
        for instance in (broken, healthy):
            instance.async_register_service = AsyncMock()
            instance.async_unregister_service = AsyncMock()
            instance.async_close = AsyncMock()
        broken.async_register_service.side_effect = OSError("interface lost")
        constructor.side_effect = [broken, healthy]
        a = _dynamic_advertiser()
        await a.start()
        constructor.assert_not_called()
        snapshot.return_value = (("wlan0", 3, "10.0.0.2", 24),)
        import pytest
        with pytest.raises(OSError):
            await a.refresh_interfaces()
        broken.async_close.assert_awaited_once()
        assert a._aiozc is None
        await a.refresh_interfaces()
        assert a._aiozc is healthy
        snapshot.return_value = ()
        await a.refresh_interfaces()
        healthy.async_close.assert_awaited_once()
        assert a._info is None
        await a.stop()


async def test_change_during_registration_is_not_adopted() -> None:
    from hub.adapters.discovery import zeroconf as m
    instance = MagicMock()
    instance.async_register_service = AsyncMock()
    instance.async_unregister_service = AsyncMock()
    instance.async_close = AsyncMock()
    with patch.object(m, "AsyncZeroconf", return_value=instance), patch.object(
        m, "interface_snapshot", side_effect=[(("wlan0", 3, "10.0.0.2", 24),), ()]
    ):
        a = _dynamic_advertiser()
        await a.start()
        assert a._aiozc is None
        instance.async_close.assert_awaited_once()
        await a.stop()


def test_the_operators_cable_is_not_one_of_the_names_two_a_records() -> None:
    """2026-09-15: one name, two A records, and the device took the wrong one.

    Both addresses here are ordinary routable /24s — which is exactly why no
    amount of looking at interfaces could have told them apart. 10.42.0.2 is a
    point-to-point cable to a workstation; a device on Wi-Fi can never route to
    it, and the Hub had no way to know that until Ops said so.
    """

    import ipaddress

    from hub.adapters.discovery import zeroconf as module

    class _Adapter:
        def __init__(self, ips):
            self.ips = ips

    class _IP:
        def __init__(self, ip):
            self.ip = ip

    adapters = [
        _Adapter([_IP("192.168.100.19")]),
        _Adapter([_IP("10.42.0.2")]),
    ]
    cable = (ipaddress.ip_network("10.42.0.0/24"),)
    with patch.object(module.ifaddr, "get_adapters", return_value=adapters):
        assert module.interface_addresses() == ("10.42.0.2", "192.168.100.19")
        assert module.interface_addresses(cable) == ("192.168.100.19",)


async def test_the_answering_surface_is_the_same_set_as_the_claim() -> None:
    """Publishing an address on one link and answering on another tells a
    device to come back somewhere it has just shown it cannot reach."""

    from hub.adapters.discovery import zeroconf as m

    instance = MagicMock()
    for method in ("async_register_service", "async_unregister_service", "async_close"):
        setattr(instance, method, AsyncMock())
    with patch.object(m, "AsyncZeroconf", return_value=instance) as constructor, patch.object(
        m, "interface_snapshot", return_value=(("wlan0", 3, "192.168.100.19", 24),)
    ):
        advertiser = _dynamic_advertiser()
        await advertiser.start()
        await advertiser.stop()

    assert constructor.call_args.kwargs["interfaces"] == ["192.168.100.19"]


async def test_plugging_in_the_operators_cable_does_not_rebuild_the_advertisement() -> None:
    """It is not a new place the product can be reached, so it is not a change.

    The transport is torn down and rebuilt on every genuine change, which drops
    the multicast memberships and re-announces. Doing that because a
    workstation was plugged in would cost every device on the LAN a
    re-resolution for nothing.
    """

    import ipaddress

    from hub.adapters.discovery import zeroconf as m

    class _Adapter:
        def __init__(self, ips):
            self.ips = ips

    class _IP:
        def __init__(self, ip):
            self.ip = ip

    instances = []

    def construct(**_kwargs):
        instance = MagicMock()
        for method in ("async_register_service", "async_unregister_service", "async_close"):
            setattr(instance, method, AsyncMock())
        instances.append(instance)
        return instance

    wifi_only = [_Adapter([_IP("192.168.100.19")])]
    with_cable = [*wifi_only, _Adapter([_IP("10.42.0.2")])]
    adapters = list(wifi_only)
    with patch.object(m, "AsyncZeroconf", side_effect=construct), patch.object(
        m.ifaddr, "get_adapters", side_effect=lambda: adapters
    ):
        advertiser = ZeroconfAuthorityCandidateAdvertiser(
            advertisement_id="test",
            service_type="_eidolon-owner._tcp.local.",
            service_name="test._eidolon-owner._tcp.local.",
            hostname="test",
            port=9443,
            owner_domain_id="test",
            owner_domain_descriptor_uri="https://test.local:9443/descriptor",
            management_networks=(ipaddress.ip_network("10.42.0.0/24"),),
            refresh_seconds=0,
        )
        await advertiser.start()
        assert len(instances) == 1

        adapters = with_cable
        await advertiser.refresh_interfaces()
        assert len(instances) == 1
        assert advertiser._info.parsed_addresses() == ["192.168.100.19"]
        await advertiser.stop()
