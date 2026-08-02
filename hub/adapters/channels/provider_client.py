"""HTTP adapter for the single external Channel Provider control contract."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Protocol

import httpx

from hub.contracts.bindings.channel import (
    ProviderChannelAcquisitionRequest,
    ProviderChannelAcquisitionResponse,
    ProviderChannelDevice,
)
from hub.contracts.bindings.device import DeviceManifest
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    ChannelState,
    OpaqueChannelBinding,
    ProviderDeviceContext,
)


class RequestReplyClient(Protocol):
    async def request(self, route: str, payload: bytes, *, timeout: float) -> bytes: ...


class HttpRequestReplyClient:
    """Small authenticated HTTP transport used by Provider control adapters."""

    def __init__(self, client: httpx.AsyncClient, *, bearer_token: str = "") -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}

    async def request(self, route: str, payload: bytes, *, timeout: float) -> bytes:
        try:
            response = await self._client.post(
                route,
                content=payload,
                headers={"Content-Type": "application/json", **self._headers},
                timeout=timeout,
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            raise ConnectionError("Channel Provider unavailable") from exc


class ChannelProviderHttpClient:
    """Uses fixed contract paths; Provider responses cannot redirect Hub calls."""

    def __init__(
        self,
        client: RequestReplyClient,
        *,
        contract_url: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not contract_url.startswith(("http://", "https://")):
            raise ValueError("Channel Provider contract_url must be HTTP(S)")
        self._client = client
        self._route = f"{contract_url.rstrip('/')}/device-channels/acquire"
        self._timeout = timeout_seconds

    async def acquire_channels(self, context: ProviderDeviceContext) -> ChannelAssignmentSet:
        request = ProviderChannelAcquisitionRequest(
            operation_id=context.operation_id,
            hub_id=context.hub_id,
            device=ProviderChannelDevice(
                device_id=context.device_id,
                public_key_fingerprint=context.public_key_fingerprint,
                tenant_id=context.tenant_id,
                owner_id=context.owner_id,
                display_name=context.display_name,
                device_kind=context.device_kind,
                manifest=DeviceManifest.model_validate_json(context.manifest_json),
                manifest_revision=context.manifest_revision,
                approved=context.approved,
                revoked=context.revoked,
                connected=context.connected,
            ),
        )
        payload = request.model_dump_json().encode()
        response = ProviderChannelAcquisitionResponse.model_validate_json(
            await self._client.request(self._route, payload, timeout=self._timeout)
        )
        grants = tuple(
            ChannelGrant(
                operation_id=response.operation_id,
                lease=ChannelLease(
                    channel_id=item.channel_id,
                    device_id=response.device_id,
                    purpose=item.purpose,
                    kinds=frozenset(ChannelKind(kind) for kind in item.kinds),
                    binding_format=item.binding_format,
                    issued_at=datetime.fromtimestamp(item.issued_at_ms / 1000, tz=UTC),
                    expires_at=datetime.fromtimestamp(item.expires_at_ms / 1000, tz=UTC),
                    state=ChannelState.PENDING,
                ),
                opaque_binding=OpaqueChannelBinding(
                    base64.b64decode(item.opaque_binding, validate=True)
                ),
            )
            for item in response.channels
        )
        return ChannelAssignmentSet(
            operation_id=response.operation_id,
            device_id=response.device_id,
            manifest_revision=response.manifest_revision,
            grants=grants,
        )
