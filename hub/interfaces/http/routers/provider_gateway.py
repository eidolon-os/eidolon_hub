"""Authenticated ingress from external channel Providers."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError

from hub.application.use_cases.ingest_data_envelope import DataEnvelopeRejected
from hub.contracts.bindings.channel import ChannelLifecycleEvent
from hub.contracts.mappers import channel_lifecycle_to_domain
from hub.ports.channels import ChannelLifecycleIngress, DataEnvelopeIngress


@dataclass(frozen=True, slots=True)
class ProviderGatewayHttpServices:
    bridge: DataEnvelopeIngress
    lifecycle: ChannelLifecycleIngress
    bearer_token: str


def create_provider_gateway_router(
    services: ProviderGatewayHttpServices | Callable[[], ProviderGatewayHttpServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/provider/v1", tags=["channel-provider"])

    def current() -> ProviderGatewayHttpServices:
        return services() if callable(services) else services

    @router.post("/data/inbound")
    async def ingest_provider_envelope(
        request: Request,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, bool]:
        runtime = current()
        expected = f"Bearer {runtime.bearer_token}"
        if not runtime.bearer_token or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="valid Provider credential required")
        try:
            accepted = await runtime.bridge.ingest_raw(await request.body())
        except (ValidationError, ValueError, PermissionError, DataEnvelopeRejected) as exc:
            raise HTTPException(status_code=422, detail=type(exc).__name__) from exc
        return {"accepted": accepted}

    @router.post("/channels/lifecycle")
    async def record_channel_lifecycle(
        payload: ChannelLifecycleEvent,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, bool]:
        runtime = current()
        expected = f"Bearer {runtime.bearer_token}"
        if not runtime.bearer_token or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="valid Provider credential required")
        try:
            await runtime.lifecycle.execute(channel_lifecycle_to_domain(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="channel not found") from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=422, detail=type(exc).__name__) from exc
        return {"accepted": True}

    return router
