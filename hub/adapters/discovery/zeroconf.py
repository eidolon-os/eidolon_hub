"""Multi-interface mDNS/DNS-SD Hub descriptor advertisement."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from contextlib import suppress

import ifaddr
from zeroconf import InterfaceChoice, IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__)


def interface_addresses() -> tuple[str, ...]:
    """Return usable IPv4/IPv6 addresses from every active interface."""
    values: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for item in adapter.ips:
            raw = item.ip[0] if isinstance(item.ip, tuple) else item.ip
            try:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or address.is_multicast:
                continue
            # A scoped link-local IPv6 literal is not a portable descriptor URI.
            if address.version == 6 and address.is_link_local:
                continue
            values.add(str(address))
    return tuple(sorted(values, key=lambda value: (ipaddress.ip_address(value).version, value)))


class ZeroconfHubAdvertiser:
    """Advertise one logical Hub on all interfaces; never bridges VLANs."""

    def __init__(
        self,
        *,
        advertisement_id: str,
        service_type: str,
        service_name: str,
        hostname: str,
        port: int,
        descriptor_uri: str,
        registration_uri: str,
        addresses: tuple[str, ...] | None = None,
        refresh_seconds: float = 10.0,
    ) -> None:
        self._advertisement_id = advertisement_id
        self._service_type = service_type
        self._service_name = service_name
        self._hostname = hostname
        self._port = port
        self._descriptor_uri = descriptor_uri
        self._registration_uri = registration_uri
        self._addresses = addresses
        self._refresh_seconds = refresh_seconds
        self._aiozc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def advertisement_id(self) -> str:
        return self._advertisement_id

    def _build_info(self, addresses: tuple[str, ...]) -> ServiceInfo:
        if not addresses:
            raise RuntimeError("mDNS advertisement has no usable interface addresses")
        return ServiceInfo(
            type_=self._service_type,
            name=self._service_name,
            addresses=[ipaddress.ip_address(value).packed for value in addresses],
            port=self._port,
            properties={
                "txtvers": "1",
                "descriptor_uri": self._descriptor_uri,
                "register_uri": self._registration_uri,
            },
            server=f"{self._hostname.rstrip('.')}.local.",
        )

    async def start(self) -> None:
        if self._aiozc is not None:
            return
        addresses = self._addresses or interface_addresses()
        aiozc = AsyncZeroconf(
            interfaces=InterfaceChoice.All,
            ip_version=IPVersion.All,
        )
        info = self._build_info(addresses)
        try:
            await aiozc.async_register_service(info, allow_name_change=False)
        except Exception:
            await aiozc.async_close()
            raise
        self._aiozc = aiozc
        self._info = info
        if self._addresses is None and self._refresh_seconds > 0:
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name=f"zeroconf-refresh:{self._advertisement_id}"
            )

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds)
                addresses = interface_addresses()
                if self._info is not None and set(self._info.parsed_addresses()) != set(addresses):
                    await self.refresh_interfaces()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Interface churn must not silently kill the long-lived
                # advertiser. Payloads and descriptor URIs are intentionally
                # omitted from this operational log.
                logger.warning("mDNS interface refresh failed reason=%s", type(exc).__name__)

    async def refresh_interfaces(self) -> None:
        if self._aiozc is None:
            raise RuntimeError("zeroconf advertiser is not started")
        addresses = self._addresses or interface_addresses()
        updated = self._build_info(addresses)
        await self._aiozc.async_update_service(updated)
        self._info = updated

    async def stop(self) -> None:
        task, self._refresh_task = self._refresh_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        aiozc, info = self._aiozc, self._info
        self._aiozc = None
        self._info = None
        if aiozc is None:
            return
        try:
            if info is not None:
                await aiozc.async_unregister_service(info)
        finally:
            await aiozc.async_close()
