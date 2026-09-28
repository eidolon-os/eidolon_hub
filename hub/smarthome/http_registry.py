"""Read home metadata from Data using Hub's managed HTTP resource."""

from urllib.parse import quote

import httpx
from eidolon_sdk.biz.smarthome import Registry


class HttpRegistrySource:
    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str):
        self._client, self._base_url, self._token = client, base_url.rstrip("/"), token

    async def get(self, owner_id: str) -> Registry:
        response = await self._client.get(
            self._base_url
            + f"/api/workspace-authority/v1/owners/{quote(owner_id, safe='')}/smarthome/registry",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=5,
        )
        response.raise_for_status()
        return Registry.model_validate(response.json())
