"""mDNS service registration for LAN discovery (RFC 6763 DNS-SD)."""

from __future__ import annotations

import logging
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_eidolon-hub._tcp.local."
SERVICE_NAME = f"Eidolon Hub.{SERVICE_TYPE}"
DEFAULT_HOSTNAME = "eidolon-hub"


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
) -> AsyncIterator[None]:
    """Async context manager that registers/unregisters the mDNS service.

    Args:
        port: Hub HTTP port.
        version: Hub software version string.
    """
    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    ip = _local_ipv4()

    info = ServiceInfo(
        type_=SERVICE_TYPE,
        name=SERVICE_NAME,
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={
            "txtvers": "1",
            "version": version,
            "api": "v1",
            "config_url": f"http://{ip}:{port}/api/esp32/config",
        },
        server=f"{DEFAULT_HOSTNAME}.local.",
    )

    try:
        await aiozc.async_register_service(info)
        logger.info("mDNS registered: %s.local:%d", DEFAULT_HOSTNAME, port)
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
