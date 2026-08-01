"""Provider-neutral channel provisioner over an HTTP control contract."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Protocol

import httpx

from hub.domain.channels.entities import (
    ChannelGrant,
    ChannelLease,
    ChannelRequest,
    OpaqueChannelBinding,
)


class RequestReplyClient(Protocol):
    async def request(self, route: str, payload: bytes, *, timeout: float) -> bytes: ...


class HttpRequestReplyClient:
    """Narrow HTTP transport shared by provisioner adapters."""

    def __init__(self, client: httpx.AsyncClient, *, bearer_token: str = "") -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}

    async def request(self, route: str, payload: bytes, *, timeout: float) -> bytes:
        response = await self._client.post(
            route,
            content=payload,
            headers={"Content-Type": "application/json", **self._headers},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.content


class ProvisionerClient:
    """Calls one externally configured Provider without protocol-specific fields."""

    def __init__(
        self,
        client: RequestReplyClient,
        *,
        route: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        if not route.startswith(("http://", "https://")):
            raise ValueError("provisioner route must be an HTTP(S) URI")
        self._route = route
        self._timeout = timeout_seconds

    async def provision(self, request: ChannelRequest) -> ChannelGrant:
        payload = json.dumps(
            {
                "operation": "channel.provision",
                "request_id": request.request_id,
                "device_id": request.device_id,
                "profile_name": request.profile.name,
                "required_kinds": sorted(kind.value for kind in request.profile.required_kinds),
                "expires_at": request.expires_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        raw = await self._client.request(self._route, payload, timeout=self._timeout)
        return self._decode_grant(raw)

    @staticmethod
    def _decode_grant(raw: bytes) -> ChannelGrant:
        response = json.loads(raw)
        binding = base64.b64decode(response["opaque_binding_b64"], validate=True)
        lease = ChannelLease(
            channel_id=response["channel_id"],
            device_id=response["device_id"],
            profile_name=response["profile_name"],
            issued_at=datetime.fromisoformat(response["issued_at"]),
            expires_at=datetime.fromisoformat(response["expires_at"]),
            renew_after=(
                datetime.fromisoformat(response["renew_after"])
                if response.get("renew_after")
                else None
            ),
        )
        return ChannelGrant(
            request_id=response["request_id"],
            lease=lease,
            opaque_binding=OpaqueChannelBinding(binding),
        )

    async def renew(self, lease: ChannelLease) -> ChannelGrant:
        payload = json.dumps(
            {
                "operation": "channel.renew",
                "channel_id": lease.channel_id,
                "device_id": lease.device_id,
                "profile_name": lease.profile_name,
            },
            sort_keys=True,
        ).encode()
        raw = await self._client.request(self._route, payload, timeout=self._timeout)
        return self._decode_grant(raw)

    async def revoke(self, lease: ChannelLease, *, reason: str) -> None:
        payload = json.dumps(
            {
                "operation": "channel.revoke",
                "channel_id": lease.channel_id,
                "reason": reason,
            },
            sort_keys=True,
        ).encode()
        await self._client.request(self._route, payload, timeout=self._timeout)
