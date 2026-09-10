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


def interface_snapshot() -> tuple[tuple[str, int, str, int], ...]:
    """Addresses and their interface identities from one OS observation."""
    values = set()
    for adapter in ifaddr.get_adapters():
        for item in adapter.ips:
            raw = item.ip[0] if isinstance(item.ip, tuple) else item.ip
            try:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError:
                continue
            if (address.is_loopback or address.is_unspecified or
                    address.is_multicast or address.is_link_local):
                continue
            values.add((getattr(adapter, "name", ""), getattr(adapter, "index", 0),
                        str(address), getattr(item, "network_prefix", 0)))
    return tuple(sorted(values))


def interface_addresses() -> tuple[str, ...]:
    return tuple(sorted({entry[2] for entry in interface_snapshot()},
                        key=lambda value: (ipaddress.ip_address(value).version, value)))


class ZeroconfAuthorityCandidateAdvertiser:
    """Advertise a candidate signed directory URI; never acts as trust."""

    def __init__(
        self,
        *,
        advertisement_id: str,
        service_type: str,
        service_name: str,
        hostname: str,
        port: int,
        owner_domain_id: str,
        owner_domain_descriptor_uri: str,
        addresses: tuple[str, ...] | None = None,
        refresh_seconds: float = 10.0,
    ) -> None:
        self._advertisement_id = advertisement_id
        self._service_type = service_type
        self._service_name = service_name
        self._hostname = hostname
        self._port = port
        self._owner_domain_id = owner_domain_id
        self._owner_domain_descriptor_uri = owner_domain_descriptor_uri
        self._addresses = addresses
        self._refresh_seconds = refresh_seconds
        self._aiozc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._running = False
        self._lock = asyncio.Lock()
        self._observation: object = None

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
                "owner_domain_id": self._owner_domain_id,
                "owner_domain_descriptor_uri": self._owner_domain_descriptor_uri,
            },
            server=f"{self._hostname.rstrip('.')}.local.",
        )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            await self.refresh_interfaces()
        except Exception as exc:
            # Hub's local APIs must survive startup without an available LAN.
            logger.warning("mDNS startup deferred reason=%s", type(exc).__name__)
        if self._refresh_seconds > 0:
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name=f"zeroconf-refresh:{self._advertisement_id}"
            )

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds)
                await self.refresh_interfaces()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("mDNS interface refresh failed reason=%s", type(exc).__name__)

    async def _close_network(self) -> None:
        aiozc, info = self._aiozc, self._info
        self._aiozc = self._info = None
        self._observation = None
        if aiozc is not None:
            try:
                if info is not None:
                    await aiozc.async_unregister_service(info)
            finally:
                await aiozc.async_close()

    async def refresh_interfaces(self) -> None:
        async with self._lock:
            if not self._running:
                return
            snapshot = interface_snapshot() if self._addresses is None else self._addresses
            addresses = (tuple(sorted({entry[2] for entry in snapshot}))
                         if self._addresses is None else self._addresses)
            if self._aiozc is not None and snapshot == self._observation:
                return
            # Updating ServiceInfo leaves the old per-interface sockets and
            # multicast memberships alive. Their owner must be replaced too.
            await self._close_network()
            if not addresses:
                return
            info = self._build_info(addresses)
            aiozc = AsyncZeroconf(interfaces=InterfaceChoice.All, ip_version=IPVersion.All)
            try:
                await aiozc.async_register_service(info, allow_name_change=False)
                # Do not adopt a transport created across another network change.
                if self._addresses is None and interface_snapshot() != snapshot:
                    await aiozc.async_unregister_service(info)
                    await aiozc.async_close()
                    return
            except BaseException:
                await aiozc.async_close()
                raise
            self._aiozc, self._info, self._observation = aiozc, info, snapshot

    async def stop(self) -> None:
        self._running = False
        task, self._refresh_task = self._refresh_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            await self._close_network()
