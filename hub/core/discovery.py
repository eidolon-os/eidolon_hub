"""mDNS service registration for LAN discovery (RFC 6763 DNS-SD)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from hub.config import DiscoveryConfig, load_config

logger = logging.getLogger(__name__)

_DISCOVERY_DEFAULTS = load_config().discovery
SERVICE_TYPE = _DISCOVERY_DEFAULTS.service_type
SERVICE_NAME = _DISCOVERY_DEFAULTS.service_name or f"Eidolon Hub.{SERVICE_TYPE}"
DEFAULT_HOSTNAME = _DISCOVERY_DEFAULTS.hostname

# How often to re-check the host LAN IP and re-advertise if it moved (DHCP /
# network change), so devices that discover us never get stuck on a dead address.
_IP_REFRESH_INTERVAL_SEC = 10.0


def _local_ipv4() -> str:
    """Return the local LAN IPv4 address (not 0.0.0.0)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@asynccontextmanager
async def mdns_lifespan(
    port: int,
    version: str,
    discovery_config: DiscoveryConfig | None = None,
) -> AsyncIterator[None]:
    """Async context manager that registers/unregisters the mDNS service.

    Args:
        port: Hub HTTP port.
        version: Hub software version string.
        discovery_config: mDNS discovery configuration.
    """
    cfg = discovery_config or load_config().discovery
    service_type = cfg.service_type
    service_name = cfg.service_name or f"Eidolon Hub.{service_type}"
    hostname = cfg.hostname
    txt_version = cfg.txt_version
    api_version = cfg.api_version
    config_path = cfg.config_path

    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)

    def _service_info(ip: str) -> ServiceInfo:
        return ServiceInfo(
            type_=service_type,
            name=service_name,
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={
                "txtvers": txt_version,
                "version": version,
                "api": api_version,
                "config_url": f"http://{ip}:{port}{config_path}",
            },
            server=f"{hostname}.local.",
        )

    current_ip = _local_ipv4()
    info = _service_info(current_ip)
    registered = False
    try:
        await aiozc.async_register_service(info)
        registered = True
        logger.info("mDNS registered: %s.local:%d -> %s", hostname, port, current_ip)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "mDNS registration failed: %s — Hub continues without LAN discovery",
            exc,
        )

    async def _readvertise_on_ip_change() -> None:
        # The advertised A record and config_url are pinned to the IP captured at
        # registration time. On a DHCP renewal / network change the host IP can
        # move, leaving devices discovering a dead address (and the served
        # config_url / livekit_url, both derived from this IP, stale). Poll for IP
        # changes and re-advertise the current address so devices self-repair
        # without a Hub restart.
        nonlocal current_ip, info
        while True:
            await asyncio.sleep(_IP_REFRESH_INTERVAL_SEC)
            new_ip = _local_ipv4()
            if new_ip in (current_ip, "127.0.0.1"):
                continue
            current_ip = new_ip
            info = _service_info(new_ip)
            try:
                await aiozc.async_update_service(info)
                logger.info("mDNS re-advertised after IP change -> %s", new_ip)
            except Exception as exc:  # pragma: no cover
                logger.warning("mDNS re-advertise failed: %s", exc)

    watcher = (
        asyncio.create_task(_readvertise_on_ip_change()) if registered else None
    )

    try:
        yield
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        if registered:
            try:
                await aiozc.async_unregister_service(info)
            except Exception:  # pragma: no cover
                pass
        await aiozc.async_close()
        logger.info("mDNS unregistered")
