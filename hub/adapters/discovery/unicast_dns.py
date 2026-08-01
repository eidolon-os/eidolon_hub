"""Asynchronous Unicast DNS-SD resolver using dnspython."""

from __future__ import annotations

from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.rdatatype


@dataclass(frozen=True, slots=True)
class DnsSdHubService:
    instance_name: str
    target: str
    port: int
    addresses: tuple[str, ...]
    descriptor_uri: str | None
    registration_uri: str | None
    priority: int
    weight: int


def _txt_properties(answer: object) -> dict[str, str]:
    properties: dict[str, str] = {}
    for rdata in answer:  # type: ignore[union-attr]
        for segment in rdata.strings:
            key, separator, value = segment.partition(b"=")
            if separator:
                properties[key.decode("utf-8")] = value.decode("utf-8")
    return properties


class UnicastDnsSdResolver:
    def __init__(self, resolver: dns.asyncresolver.Resolver | None = None) -> None:
        self._resolver = resolver or dns.asyncresolver.Resolver()

    async def resolve(self, service_name: str) -> tuple[DnsSdHubService, ...]:
        ptr = await self._resolver.resolve(service_name, dns.rdatatype.PTR)
        services: list[DnsSdHubService] = []
        for record in ptr:
            instance = str(record.target).rstrip(".")
            srv = await self._resolver.resolve(instance, dns.rdatatype.SRV)
            srv_record = min(srv, key=lambda item: (item.priority, -item.weight))
            target = str(srv_record.target).rstrip(".")
            addresses: set[str] = set()
            for record_type in (dns.rdatatype.A, dns.rdatatype.AAAA):
                try:
                    answer = await self._resolver.resolve(target, record_type)
                except (dns.exception.DNSException, OSError):
                    continue
                addresses.update(str(item.address) for item in answer)
            try:
                txt = _txt_properties(await self._resolver.resolve(instance, dns.rdatatype.TXT))
            except (dns.exception.DNSException, OSError):
                txt = {}
            services.append(
                DnsSdHubService(
                    instance_name=instance,
                    target=target,
                    port=int(srv_record.port),
                    addresses=tuple(sorted(addresses)),
                    descriptor_uri=txt.get("descriptor_uri"),
                    registration_uri=txt.get("register_uri"),
                    priority=int(srv_record.priority),
                    weight=int(srv_record.weight),
                )
            )
        return tuple(
            sorted(services, key=lambda item: (item.priority, -item.weight, item.instance_name))
        )
