from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx

from hub.adapters.channels.data_bridge import HttpDataEnvelopeSender
from hub.adapters.channels.provisioner_client import HttpRequestReplyClient, ProvisionerClient
from hub.domain.channels.entities import ChannelKind, ChannelProfile, ChannelRequest

NOW = datetime(2026, 8, 1, tzinfo=UTC)


async def test_http_provisioner_uses_authenticated_contract_without_binding_inspection() -> None:
    captured = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        message = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "request_id": message["request_id"],
                "channel_id": "channel-1",
                "device_id": message["device_id"],
                "profile_name": message["profile_name"],
                "issued_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                "opaque_binding_b64": base64.b64encode(b"encrypted-binding").decode(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provisioner = ProvisionerClient(
            HttpRequestReplyClient(client, bearer_token="provider-control-secret"),
            route="https://provider.example/v1/channels/operations",
        )
        grant = await provisioner.provision(
            ChannelRequest(
                request_id="request-1",
                device_id="device-1",
                profile=ChannelProfile(
                    "management-data",
                    frozenset({ChannelKind.RELIABLE_DATA}),
                    "provider/default",
                ),
                requested_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )
        )

    assert captured[0].headers["Authorization"] == "Bearer provider-control-secret"
    assert grant.opaque_binding.relay_bytes() == b"encrypted-binding"
    assert "encrypted-binding" not in repr(grant)


async def test_http_data_sender_posts_standard_envelope_to_provider() -> None:
    captured = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = HttpDataEnvelopeSender(
            client,
            route="https://provider.example/v1/data/envelopes",
            bearer_token="provider-data-secret",
        )
        await sender.send(b'{"envelope_id":"envelope-1"}')

    assert captured[0].url.path == "/v1/data/envelopes"
    assert captured[0].headers["Authorization"] == "Bearer provider-data-secret"
