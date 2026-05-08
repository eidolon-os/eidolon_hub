"""mDNS service registration for LAN discovery (RFC 6763 DNS-SD)."""

from __future__ import annotations

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
    ip = _local_ipv4()

    info = ServiceInfo(
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

    try:
        await aiozc.async_register_service(info)
        logger.info("mDNS registered: %s.local:%d", hostname, port)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "mDNS registration failed: %s — Hub continues without LAN discovery",
            exc,
        )

    try:
        yield
    finally:
        try:
            await aiozc.async_unregister_service(info)
        except Exception:  # pragma: no cover
            pass
        await aiozc.async_close()
        logger.info("mDNS unregistered")
