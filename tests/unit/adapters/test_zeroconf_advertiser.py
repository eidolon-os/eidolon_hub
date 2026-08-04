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
