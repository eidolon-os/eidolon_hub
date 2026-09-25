"""Exact HTTP adapter for the Provider-owned v1 Channel contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hub.contracts.bindings.device import DeviceRef
from hub.contracts.bindings.presentation import DeviceOutputPolicy

from .domain import (
    ChannelBinding,
    ChannelProviderError,
    ChannelProviderUnavailable,
    CurrentChannelBinding,
)
from .specification import channel_device_payload


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ProvisionResponse(_WireModel):
    operation: Literal["channel.provisioned-device"]
    operation_id: str
    device_ref: DeviceRef
    manifest_revision: str
    output_policy: DeviceOutputPolicy | None = None
    channels: tuple[ChannelBinding, ...] = Field(min_length=1, max_length=1)


class _CurrentResponse(_WireModel):
    operation: Literal["channel.current-device"]
    binding: _ProvisionResponse | None
    refresh_required: bool = False


class _RevokeResponse(_WireModel):
    operation: Literal["channel.revoked-device"]
    operation_id: str
    device_ref: DeviceRef


class _SharedReady(_WireModel):
    session_id: str
    state: Literal["transport_ready"]
    device_ids: tuple[str, ...]


class _SharedClosed(_WireModel):
    session_id: str
    state: Literal["closed"]


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
            output_policy=binding.output_policy,
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
        output_policy: DeviceOutputPolicy | None = None,
        observed_host_address: str = "",
    ) -> tuple[ChannelBinding, ...]:
        payload = {
            "operation": operation,
            "operation_id": operation_id,
            "device_ref": device_ref.model_dump(mode="json"),
            # Beside the operation rather than inside `device`: this is not
            # something the device declared about itself, it is what this
            # Authority saw of the connection that asked. Left out altogether
            # when there was nothing to see, so that the Provider reads an
            # absent field rather than having to decide what an empty address
            # means.
            **({"observed_host_address": observed_host_address} if observed_host_address else {}),
            "device": channel_device_payload(
                owner_id=owner_id, display_name=display_name, manifest_id=manifest_id,
                manifest=manifest, manifest_revision=manifest_revision, output_policy=output_policy,
            ),
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

    async def device_conversation(self, *, action: str, owner_id: str,
                                  session_id: str, selection=None) -> dict:
        from eidolon_sdk.biz.control.device_conversation import DeviceConversationStatus
        if action not in {"open", "status", "close"}:
            raise ValueError("invalid device conversation action")
        payload = {"owner_id": owner_id}
        if action == "open":
            payload["selection"] = selection.model_dump(mode="json")
        else:
            payload["session_id"] = session_id
        raw = await self._post(f"device-conversations/{action}", payload)
        try:
            result = DeviceConversationStatus.model_validate_json(raw)
            if result.session_id != session_id:
                raise ValueError("conversation response names another session")
        except (ValidationError, ValueError) as exc:
            raise ChannelProviderError("INVALID_PROVIDER_RESPONSE", retryable=False,
                detail="invalid device conversation response") from exc
        return result.model_dump(mode="json")

    async def open_shared_session(self, *, selection, owner_id: str, specifications: list[dict]) -> dict:
        raw = await self._post("shared-sessions/open", {
            "selection": selection.model_dump(mode="json"),
            "owner_id": owner_id, "specifications": specifications,
        }, timeout_seconds=30.0)
        try:
            response = _SharedReady.model_validate_json(raw)
            if (response.session_id != selection.session_id or
                    response.device_ids != tuple(r.device_instance_id for r in selection.devices)):
                raise ValueError("shared admission differs from selected devices")
        except (ValidationError, ValueError) as exc:
            raise ChannelProviderError("INVALID_PROVIDER_RESPONSE", retryable=False,
                                       detail="invalid shared admission response") from exc
        return response.model_dump(mode="json")

    async def close_shared_session(self, *, session_id: str, owner_id: str) -> dict:
        raw = await self._post("shared-sessions/close", {
            "session_id": session_id, "owner_id": owner_id,
        }, timeout_seconds=30.0)
        try:
            response = _SharedClosed.model_validate_json(raw)
            if response.session_id != session_id:
                raise ValueError("shared close names another session")
        except (ValidationError, ValueError) as exc:
            raise ChannelProviderError("INVALID_PROVIDER_RESPONSE", retryable=False,
                                       detail="invalid shared close response") from exc
        return response.model_dump(mode="json")

    async def _post(self, route: str, payload: dict[str, object], *, timeout_seconds: float | None = None) -> bytes:
        try:
            response = await self._client.post(
                f"{self._base}/{route}",
                content=json.dumps(payload, separators=(",", ":")),
                headers={"Content-Type": "application/json", **self._headers},
                timeout=self._timeout if timeout_seconds is None else timeout_seconds,
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
