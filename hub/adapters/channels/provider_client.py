"""HTTP adapter for the single external Channel Provider control contract."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import ValidationError

from hub.contracts.bindings.channel import (
    ProviderChannelDevice,
    ProviderChannelProvisionRequest,
    ProviderChannelProvisionResponse,
    ProviderChannelRevocationRequest,
    ProviderChannelRevocationResponse,
)
from hub.contracts.bindings.device import DeviceManifest
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    OpaqueChannelBinding,
    ProviderChannelRevocation,
    ProviderDeviceContext,
)
from hub.ports.channels import ChannelProviderContractError


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
        base_url = contract_url.rstrip("/")
        self._provision_route = f"{base_url}/device-channels/provision"
        self._revoke_route = f"{base_url}/device-channels/revoke"
        self._timeout = timeout_seconds

    async def provision_channels(self, context: ProviderDeviceContext) -> ChannelAssignmentSet:
        request = ProviderChannelProvisionRequest(
            operation_id=context.operation_id,
            hub_id=context.hub_id,
            device=ProviderChannelDevice(
                device_id=context.device_id,
                owner_id=context.owner_id,
                display_name=context.display_name,
                device_kind=context.device_kind,
                manifest=DeviceManifest.model_validate_json(context.manifest_json),
                manifest_revision=context.manifest_revision,
            ),
        )
        # Serialised by alias, because the wire is the contract and this model
        # is not it: the manifest's `schema` field is `schema_` in Python, and
        # dumping without the alias sends a name the Provider has never heard
        # of. It only shows up once a device declares a property — every
        # manifest with an empty `properties` list serialises identically
        # either way — so the first device to describe itself was the first
        # one the Provider refused.
        payload = request.model_dump_json(by_alias=True).encode()
        try:
            response = ProviderChannelProvisionResponse.model_validate_json(
                await self._client.request(self._provision_route, payload, timeout=self._timeout)
            )
            grants = tuple(
                ChannelGrant(
                    channel_id=item.channel_id,
                    device_id=response.device_id,
                    purpose=item.purpose,
                    kinds=frozenset(ChannelKind(kind) for kind in item.kinds),
                    binding_format=item.binding_format,
                    issued_at=datetime.fromtimestamp(item.issued_at_ms / 1000, tz=UTC),
                    expires_at=datetime.fromtimestamp(item.expires_at_ms / 1000, tz=UTC),
                    opaque_binding=OpaqueChannelBinding(
                        base64.b64decode(item.opaque_binding, validate=True)
                    ),
                )
                for item in response.channels
            )
        except (ValidationError, ValueError) as exc:
            raise ChannelProviderContractError("invalid Provider provision response") from exc
        return ChannelAssignmentSet(
            operation_id=response.operation_id,
            device_id=response.device_id,
            manifest_revision=response.manifest_revision,
            grants=grants,
        )

    async def revoke_channels(self, revocation: ProviderChannelRevocation) -> None:
        request = ProviderChannelRevocationRequest(
            operation_id=revocation.operation_id,
            hub_id=revocation.hub_id,
            device_id=revocation.device_id,
            reason=revocation.reason,
        )
        try:
            response = ProviderChannelRevocationResponse.model_validate_json(
                await self._client.request(
                    self._revoke_route,
                    request.model_dump_json(by_alias=True).encode(),
                    timeout=self._timeout,
                )
            )
        except ValidationError as exc:
            raise ChannelProviderContractError("invalid Provider revocation response") from exc
        if (
            response.operation_id != revocation.operation_id
            or response.device_id != revocation.device_id
        ):
            raise ChannelProviderContractError(
                "Provider revocation response does not match request"
            )
