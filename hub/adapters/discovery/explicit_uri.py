"""Commissioning-time explicit Descriptor URI source."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from hub.contracts.bindings.connection import HubDescriptor


class ExplicitUriDiscovery:
    def __init__(self, http_client: httpx.AsyncClient, descriptor_uris: tuple[str, ...]) -> None:
        if not descriptor_uris:
            raise ValueError("at least one descriptor URI is required")
        for uri in descriptor_uris:
            parsed = urlparse(uri)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("explicit descriptor URI must use https")
        self._http = http_client
        self._uris = tuple(dict.fromkeys(descriptor_uris))

    async def discover(self) -> tuple[HubDescriptor, ...]:
        descriptors: list[HubDescriptor] = []
        for uri in self._uris:
            response = await self._http.get(uri)
            response.raise_for_status()
            descriptor = HubDescriptor.model_validate(response.json())
            if descriptor.descriptor_uri != uri:
                raise ValueError("Hub descriptor URI does not match commissioned URI")
            descriptors.append(descriptor)
        return tuple(descriptors)
