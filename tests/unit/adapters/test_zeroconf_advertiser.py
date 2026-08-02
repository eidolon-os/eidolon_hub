from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser


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
            descriptor_uri="https://eidolon-hub.local:8443/api/device-access/v1/descriptor",
            registration_uri="https://eidolon-hub.local:8443/api/device-access/v1/register",
            addresses=("192.168.10.5", "2001:db8::5"),
        )

        await advertiser.start()
        info = instance.async_register_service.await_args.args[0]
        await advertiser.stop()

    assert advertiser.advertisement_id == "mdns-local"
    assert set(info.parsed_addresses()) == {"192.168.10.5", "2001:db8::5"}
    assert info.properties[b"descriptor_uri"].startswith(b"https://")
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
            descriptor_uri="https://eidolon-hub.local:8443/api/device-access/v1/descriptor",
            registration_uri="https://eidolon-hub.local:8443/api/device-access/v1/register",
            addresses=("192.168.10.5",),
        )

        await advertiser.start()
        await advertiser.start()
        await advertiser.stop()
        await advertiser.stop()

    instance.async_register_service.assert_awaited_once()
    instance.async_close.assert_awaited_once()
