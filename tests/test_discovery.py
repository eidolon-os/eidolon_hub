"""Tests for hub/core/discovery.py — mDNS / DNS-SD LAN discovery (RFC 6763)."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub.config import DiscoveryConfig
from hub.core.discovery import (
    DEFAULT_HOSTNAME,
    SERVICE_NAME,
    SERVICE_TYPE,
    _local_ipv4,
    mdns_lifespan,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

class TestConstants:
    def test_service_type_format(self):
        assert SERVICE_TYPE == "_eidolon-hub._tcp.local."
        assert SERVICE_TYPE.endswith(".local.")
        assert SERVICE_TYPE.startswith("_")

    def test_service_name(self):
        assert SERVICE_NAME == f"Eidolon Hub.{SERVICE_TYPE}"
        assert SERVICE_NAME.endswith(".local.")

    def test_default_hostname(self):
        assert DEFAULT_HOSTNAME == "eidolon-hub"


# --------------------------------------------------------------------------
# _local_ipv4()
# --------------------------------------------------------------------------

class TestLocalIPv4:
    def test_returns_non_loopback_on_success(self, monkeypatch):
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("192.168.1.100",)
        mock_sock.connect.return_value = None

        def factory(*args, **kwargs):
            return mock_sock

        monkeypatch.setattr(socket, "socket", factory)
        assert _local_ipv4() == "192.168.1.100"
        mock_sock.connect.assert_called_once_with(("8.8.8.8", 80))
        mock_sock.close.assert_called_once()

    def test_returns_loopback_on_connect_failure(self, monkeypatch):
        def factory(*args, **kwargs):
            m = MagicMock()
            m.connect.side_effect = OSError("network unreachable")
            return m

        monkeypatch.setattr(socket, "socket", factory)
        assert _local_ipv4() == "127.0.0.1"


# --------------------------------------------------------------------------
# mdns_lifespan — async context manager
# --------------------------------------------------------------------------

class TestMdnsLifespan:
    @pytest.fixture
    def mock_zeroconf(self, monkeypatch):
        """Patches zeroconf imports so we can inspect ServiceInfo passed in."""
        infos_registered: list[MagicMock] = []
        infos_unregistered: list[MagicMock] = []

        mock_aiozc = MagicMock()
        mock_aiozc.async_register_service = AsyncMock(return_value=None)
        mock_aiozc.async_unregister_service = AsyncMock(return_value=None)
        mock_aiozc.async_update_service = AsyncMock(return_value=None)
        mock_aiozc.async_close = AsyncMock(return_value=None)

        async def _register(info):
            infos_registered.append(info)
            # Intercept the ServiceInfo so we can inspect it later
            return None

        async def _unregister(info):
            infos_unregistered.append(info)
            return None

        mock_aiozc.async_register_service = AsyncMock(side_effect=_register)
        mock_aiozc.async_unregister_service = AsyncMock(side_effect=_unregister)

        captured_infos: list[MagicMock] = []

        async def _register_captured(info):
            captured_infos.append(info)
            return None

        mock_aiozc.async_register_service = AsyncMock(side_effect=_register_captured)

        def fake_asynczc(**kwargs):
            return mock_aiozc

        monkeypatch.setattr("hub.core.discovery.AsyncZeroconf", fake_asynczc)
        return {
            "mock": mock_aiozc,
            "captured": captured_infos,
            "registered": infos_registered,
            "unregistered": infos_unregistered,
        }

    @pytest.mark.asyncio
    async def test_lifespan_yields_and_closes(self, mock_zeroconf):
        async with mdns_lifespan(port=8081, version="0.1.0"):
            pass  # context body
        mock_zeroconf["mock"].async_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_unregisters_on_exit(self, mock_zeroconf):
        async with mdns_lifespan(port=8081, version="0.1.0"):
            pass
        assert len(mock_zeroconf["captured"]) == 1
        mock_zeroconf["mock"].async_unregister_service.assert_awaited_once_with(
            mock_zeroconf["captured"][0]
        )

    @pytest.mark.asyncio
    async def test_readvertises_on_ip_change(self, mock_zeroconf, monkeypatch):
        # When the host LAN IP moves (DHCP / network change) the advertisement
        # must follow it, so devices that discover us never get a dead address.
        monkeypatch.setattr("hub.core.discovery._IP_REFRESH_INTERVAL_SEC", 0.01)
        ips = iter(["192.168.3.150", "192.168.3.152"])
        monkeypatch.setattr(
            "hub.core.discovery._local_ipv4",
            lambda: next(ips, "192.168.3.152"),
        )
        update = mock_zeroconf["mock"].async_update_service
        async with mdns_lifespan(port=8081, version="0.1.0"):
            for _ in range(50):
                if update.await_count:
                    break
                await asyncio.sleep(0.01)
        update.assert_awaited()
        new_info = update.await_args.args[0]
        assert socket.inet_aton("192.168.3.152") in new_info.addresses
        assert b"192.168.3.152" in new_info.properties[b"config_url"]

    @pytest.mark.asyncio
    async def test_does_not_readvertise_when_ip_stable(self, mock_zeroconf, monkeypatch):
        monkeypatch.setattr("hub.core.discovery._IP_REFRESH_INTERVAL_SEC", 0.01)
        monkeypatch.setattr("hub.core.discovery._local_ipv4", lambda: "192.168.3.152")
        async with mdns_lifespan(port=8081, version="0.1.0"):
            await asyncio.sleep(0.05)
        mock_zeroconf["mock"].async_update_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_registers_ipv4_only_zeroconf(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock()
            mock_instance.async_unregister_service = AsyncMock()
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            async with mdns_lifespan(port=8081, version="0.1.0"):
                pass

            MockAZC.assert_called_once()
            call_kwargs = MockAZC.call_args.kwargs
            assert "ip_version" in call_kwargs

    @pytest.mark.asyncio
    async def test_lifespan_registers_on_entry(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock()
            mock_instance.async_unregister_service = AsyncMock()
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            async with mdns_lifespan(port=9000, version="2.3.4"):
                pass

            mock_instance.async_register_service.assert_awaited_once()
            info = mock_instance.async_register_service.await_args.args[0]
            assert info.port == 9000
            assert info.server == "eidolon-hub.local."

    @pytest.mark.asyncio
    async def test_lifespan_does_not_propagate_registration_error(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock(
                side_effect=RuntimeError("network unavailable")
            )
            mock_instance.async_unregister_service = AsyncMock()
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            # Should not raise — registration failure is swallowed
            async with mdns_lifespan(port=8081, version="0.1.0"):
                pass

            mock_instance.async_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_closes_even_if_unregister_fails(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock()
            mock_instance.async_unregister_service = AsyncMock(
                side_effect=RuntimeError("unregister failed")
            )
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            # Should not raise — unregister failure is silently ignored
            async with mdns_lifespan(port=8081, version="0.1.0"):
                pass

            mock_instance.async_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_service_info_txt_fields(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            with patch("hub.core.discovery._local_ipv4", return_value="192.168.1.100"):
                mock_instance = MagicMock()
                mock_instance.async_register_service = AsyncMock()
                mock_instance.async_unregister_service = AsyncMock()
                mock_instance.async_close = AsyncMock()
                MockAZC.return_value = mock_instance

                async with mdns_lifespan(
                    port=8081,
                    version="1.2.3",
                    discovery_config=DiscoveryConfig(config_path="/api/config"),
                ):
                    info = mock_instance.async_register_service.await_args.args[0]
                    props = info.properties

                    assert props[b"txtvers"] == b"1"
                    assert props[b"version"] == b"1.2.3"
                    assert props[b"api"] == b"v1"
                    assert props[b"config_url"] == b"http://192.168.1.100:8081/api/config"

    @pytest.mark.asyncio
    async def test_service_info_server_name(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock()
            mock_instance.async_unregister_service = AsyncMock()
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            async with mdns_lifespan(port=8081, version="0.1.0"):
                info = mock_instance.async_register_service.await_args.args[0]
                assert info.server == f"{DEFAULT_HOSTNAME}.local."

    @pytest.mark.asyncio
    async def test_service_info_type_and_name(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock()
            mock_instance.async_unregister_service = AsyncMock()
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            async with mdns_lifespan(port=8081, version="0.1.0"):
                info = mock_instance.async_register_service.await_args.args[0]
                assert info.type == SERVICE_TYPE
                assert info.name == SERVICE_NAME

    @pytest.mark.asyncio
    async def test_service_info_uses_local_ipv4(self, mock_zeroconf):
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            with patch("hub.core.discovery._local_ipv4", return_value="10.0.0.5"):
                mock_instance = MagicMock()
                mock_instance.async_register_service = AsyncMock()
                mock_instance.async_unregister_service = AsyncMock()
                mock_instance.async_close = AsyncMock()
                MockAZC.return_value = mock_instance

                async with mdns_lifespan(port=8081, version="0.1.0"):
                    info = mock_instance.async_register_service.await_args.args[0]
                    # addresses is a list of bytes (packed IPv4)
                    assert len(info.addresses) == 1
                    assert socket.inet_ntoa(info.addresses[0]) == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_lifespan_nests_inside_other_async_context(self):
        """Verify the context manager composes correctly with outer contexts."""
        with patch("hub.core.discovery.AsyncZeroconf") as MockAZC:
            mock_instance = MagicMock()
            mock_instance.async_register_service = AsyncMock()
            mock_instance.async_unregister_service = AsyncMock()
            mock_instance.async_close = AsyncMock()
            MockAZC.return_value = mock_instance

            outer_entered = False
            outer_exited = False

            @asynccontextmanager
            async def fake_outer():
                nonlocal outer_entered, outer_exited
                outer_entered = True
                yield
                outer_exited = True

            async with fake_outer():
                async with mdns_lifespan(port=8081, version="0.1.0"):
                    # Outer is entered, inner is still active, outer hasn't exited yet
                    assert outer_entered
                    assert not outer_exited

            # After both contexts exit, outer should be marked exited
            assert outer_exited
