"""mDNS service registration for LAN discovery (RFC 6763 DNS-SD)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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


@dataclass
class MdnsDiscoverySnapshot:
    service_type: str
    service_name: str
    hostname: str
    port: int
    registered: bool = False
    ip: str = ""
    register_url: str = ""
    last_registered_at: str | None = None
    last_updated_at: str | None = None
    last_error: str = ""


class MdnsDiscoveryState:
    """In-memory status for Hub's LAN discovery advertisement."""

    def __init__(self) -> None:
        self._snapshot = MdnsDiscoverySnapshot(
            service_type=SERVICE_TYPE,
            service_name=SERVICE_NAME,
            hostname=DEFAULT_HOSTNAME,
            port=0,
        )

    def configure(
        self,
        *,
        service_type: str,
        service_name: str,
        hostname: str,
        port: int,
        ip: str,
        register_url: str,
    ) -> None:
        self._snapshot = MdnsDiscoverySnapshot(
            service_type=service_type,
            service_name=service_name,
            hostname=hostname,
            port=port,
            registered=False,
            ip=ip,
            register_url=register_url,
            last_updated_at=_utc_now_iso(),
        )

    def mark_registered(self) -> None:
        now = _utc_now_iso()
        self._snapshot.registered = True
        self._snapshot.last_registered_at = now
        self._snapshot.last_updated_at = now
        self._snapshot.last_error = ""

    def mark_updated(self, *, ip: str, register_url: str) -> None:
        self._snapshot.ip = ip
        self._snapshot.register_url = register_url
        self._snapshot.registered = True
        self._snapshot.last_updated_at = _utc_now_iso()
        self._snapshot.last_error = ""

    def mark_error(self, exc: Exception) -> None:
        self._snapshot.registered = False
        self._snapshot.last_updated_at = _utc_now_iso()
        self._snapshot.last_error = str(exc)

    def mark_unavailable(self, message: str) -> None:
        self._snapshot.registered = False
        self._snapshot.register_url = ""
        self._snapshot.last_updated_at = _utc_now_iso()
        self._snapshot.last_error = message

    def mark_stopped(self) -> None:
        self._snapshot.registered = False
        self._snapshot.last_updated_at = _utc_now_iso()

    def snapshot(self) -> dict:
        return asdict(self._snapshot)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _is_advertisable_ipv4(ip: str) -> bool:
    return bool(ip) and ip != "0.0.0.0" and not ip.startswith("127.")


@asynccontextmanager
async def mdns_lifespan(
    port: int,
    version: str,
    discovery_config: DiscoveryConfig | None = None,
    discovery_state: MdnsDiscoveryState | None = None,
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

    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)

    def _register_url(ip: str) -> str:
        return f"http://{ip}:{port}/api/device/register"

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
                "register_url": _register_url(ip),
            },
            server=f"{hostname}.local.",
        )

    current_ip = ""
    info: ServiceInfo | None = None
    registered = False

    def _configure_state(ip: str) -> None:
        if discovery_state is not None:
            discovery_state.configure(
                service_type=service_type,
                service_name=service_name,
                hostname=hostname,
                port=port,
                ip=ip if _is_advertisable_ipv4(ip) else "",
                register_url=_register_url(ip) if _is_advertisable_ipv4(ip) else "",
            )

    async def _unregister_current() -> None:
        nonlocal registered
        if not registered or info is None:
            registered = False
            return
        try:
            await aiozc.async_unregister_service(info)
        except Exception as exc:  # pragma: no cover
            logger.warning("mDNS unregister failed during network update: %s", exc)
        registered = False

    async def _register(ip: str) -> None:
        nonlocal current_ip, info, registered
        current_ip = ip
        _configure_state(ip)
        if not _is_advertisable_ipv4(ip):
            await _unregister_current()
            if discovery_state is not None:
                discovery_state.mark_unavailable("no LAN IPv4 address available")
            logger.warning("mDNS not registered: no LAN IPv4 address available")
            return

        info = _service_info(ip)
        try:
            await aiozc.async_register_service(info)
            registered = True
            if discovery_state is not None:
                discovery_state.mark_registered()
            logger.info("mDNS registered: %s.local:%d -> %s", hostname, port, ip)
        except Exception as exc:  # pragma: no cover
            registered = False
            if discovery_state is not None:
                discovery_state.mark_error(exc)
            logger.warning(
                "mDNS registration failed: %s — will retry while Hub is running",
                exc,
            )

    async def _move_advertisement(ip: str) -> None:
        nonlocal current_ip, info, registered
        if not _is_advertisable_ipv4(ip):
            current_ip = ip
            await _unregister_current()
            if discovery_state is not None:
                discovery_state.mark_unavailable("no LAN IPv4 address available")
            logger.warning("mDNS suspended: no LAN IPv4 address available")
            return

        if not registered or info is None:
            await _register(ip)
            return

        new_info = _service_info(ip)
        try:
            await aiozc.async_update_service(new_info)
            current_ip = ip
            info = new_info
            if discovery_state is not None:
                discovery_state.mark_updated(
                    ip=ip,
                    register_url=_register_url(ip),
                )
            logger.info("mDNS re-advertised after IP change -> %s", ip)
        except Exception as exc:  # pragma: no cover
            logger.warning("mDNS update failed; rebuilding advertisement: %s", exc)
            await _unregister_current()
            await _register(ip)

    await _register(_local_ipv4())

    async def _maintain_advertisement() -> None:
        # The advertised A record and register_url are pinned to the IP captured at
        # registration time. On a DHCP renewal / network change the host IP can
        # move, leaving devices discovering a dead address (and the advertised
        # register_url stale). Poll and keep the advertisement
        # aligned with the current LAN address; also retry after startup failure.
        while True:
            await asyncio.sleep(_IP_REFRESH_INTERVAL_SEC)
            new_ip = _local_ipv4()
            if registered and new_ip == current_ip:
                continue
            await _move_advertisement(new_ip)

    watcher = asyncio.create_task(_maintain_advertisement())

    try:
        yield
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        await _unregister_current()
        if discovery_state is not None:
            discovery_state.mark_stopped()
        await aiozc.async_close()
        logger.info("mDNS unregistered")
