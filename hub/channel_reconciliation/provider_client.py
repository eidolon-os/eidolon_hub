"""Exact HTTP adapter for the Provider-owned v1 Channel contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hub.contracts.bindings.device import DeviceRef

from .domain import (
    ChannelBinding,
    ChannelProviderError,
    ChannelProviderUnavailable,
    CurrentChannelBinding,
)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ProvisionResponse(_WireModel):
    operation: Literal["channel.provisioned-device"]
    operation_id: str
    device_ref: DeviceRef
    manifest_revision: str
    channels: tuple[ChannelBinding, ...] = Field(min_length=1, max_length=1)


class _CurrentResponse(_WireModel):
    operation: Literal["channel.current-device"]
    binding: _ProvisionResponse | None
    refresh_required: bool = False


class _RevokeResponse(_WireModel):
    operation: Literal["channel.revoked-device"]
    operation_id: str
    device_ref: DeviceRef


class ChannelProviderHttpClient:
    """Provider client that never turns domain rejection into transport failure."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token: str,
        contract_url: str = "http://127.0.0.1:8767/v1",
        timeout_seconds: float = 10.0,
    ) -> None:
        if len(token.encode()) < 32:
            raise ValueError("Channel Provider token must contain at least 32 bytes")
        parsed = httpx.URL(contract_url)
        if parsed.scheme not in {"http", "https"} or parsed.query or parsed.fragment:
            raise ValueError("Channel Provider contract URL must be plain HTTP(S)")
        self._client = client
        self._base = contract_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_seconds

    async def current(self, *, device_ref: DeviceRef) -> CurrentChannelBinding | None:
        """What the Provider holds for this device now, without changing it."""

        raw = await self._post(
            "device-channels/current",
            {
                "operation": "channel.current-device",
                "device_ref": device_ref.model_dump(mode="json"),
            },
        )
        try:
            response = _CurrentResponse.model_validate_json(raw)
        except ValidationError as exc:
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE",
                retryable=False,
                detail="invalid current binding response",
            ) from exc
        binding = response.binding
        if binding is None:
            return None
        if binding.device_ref != device_ref:
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE",
                retryable=False,
                detail="current binding response names another device",
            )
        return CurrentChannelBinding(
            operation_id=binding.operation_id,
            manifest_revision=binding.manifest_revision,
            channels=binding.channels,
            refresh_required=response.refresh_required,
        )

    async def provision(self, **values) -> tuple[ChannelBinding, ...]:
        return await self._provision("channel.provision-device", **values)

    async def refresh(self, **values) -> tuple[ChannelBinding, ...]:
        return await self._provision("channel.refresh-device", **values)

    async def _provision(
        self,
        operation: str,
        *,
        operation_id: str,
        device_ref: DeviceRef,
        owner_id: str,
        display_name: str,
        manifest_id: str,
        manifest: Mapping[str, object],
        manifest_revision: str,
    ) -> tuple[ChannelBinding, ...]:
        payload = {
            "operation": operation,
            "operation_id": operation_id,
            "device_ref": device_ref.model_dump(mode="json"),
            "device": {
                "owner_id": owner_id,
                "display_name": display_name,
                "device_kind": manifest_id,
                "manifest": dict(manifest),
                "manifest_revision": manifest_revision,
            },
        }
        raw = await self._post("device-channels/provision", payload)
        try:
            response = _ProvisionResponse.model_validate_json(raw)
        except ValidationError as exc:
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE", retryable=False, detail="invalid provision response"
            ) from exc
        if (
            response.operation_id != operation_id
            or response.device_ref != device_ref
            or response.manifest_revision != manifest_revision
        ):
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE",
                retryable=False,
                detail="provision response does not match request",
            )
        return response.channels

    async def revoke(self, *, operation_id: str, device_ref: DeviceRef, reason: str) -> None:
        raw = await self._post(
            "device-channels/revoke",
            {
                "operation": "channel.revoke-device",
                "operation_id": operation_id,
                "device_ref": device_ref.model_dump(mode="json"),
                "reason": reason,
            },
        )
        try:
            response = _RevokeResponse.model_validate_json(raw)
        except ValidationError as exc:
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE", retryable=False, detail="invalid revoke response"
            ) from exc
        if response.operation_id != operation_id or response.device_ref != device_ref:
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE",
                retryable=False,
                detail="revoke response does not match request",
            )

    async def _post(self, route: str, payload: dict[str, object]) -> bytes:
        try:
            response = await self._client.post(
                f"{self._base}/{route}",
                content=json.dumps(payload, separators=(",", ":")),
                headers={"Content-Type": "application/json", **self._headers},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ChannelProviderUnavailable() from exc
        if response.is_success:
            return response.content
        try:
            problem = response.json()
            code = str(problem["code"])
            retryable = bool(problem["retryable"])
            authority = problem["authority"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE",
                retryable=response.status_code >= 500,
                detail=f"Provider returned HTTP {response.status_code}",
            ) from exc
        if authority != "eidolon-channel-provider":
            raise ChannelProviderError(
                "INVALID_PROVIDER_RESPONSE", retryable=False, detail="wrong problem authority"
            )
        raise ChannelProviderError(
            code, retryable=retryable, detail=str(problem.get("detail", code))
        )
