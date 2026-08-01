from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.interfaces.http.routers.provider_gateway import (
    ProviderGatewayHttpServices,
    create_provider_gateway_router,
)


class _Bridge:
    async def ingest_raw(self, raw):
        self.raw = raw
        return True


class _Lifecycle:
    def __init__(self):
        self.values = []

    async def execute(self, lifecycle):
        self.values.append(lifecycle)


def _client():
    bridge, lifecycle = _Bridge(), _Lifecycle()
    app = FastAPI()
    app.include_router(
        create_provider_gateway_router(
            ProviderGatewayHttpServices(
                bridge=bridge,
                lifecycle=lifecycle,
                bearer_token="provider-contract-secret",
            )
        )
    )
    return TestClient(app), bridge, lifecycle


def test_provider_gateway_fails_closed_without_credential() -> None:
    client, _bridge, _lifecycle = _client()

    assert client.post("/api/provider/v1/data/inbound", content=b"{}").status_code == 401
    assert (
        client.post(
            "/api/provider/v1/channels/lifecycle",
            json={
                "operation": "channel.lifecycle",
                "channel_id": "channel-1",
                "device_id": "device-1",
                "state": "active",
                "occurred_at_ms": 1,
                "reason": "",
            },
        ).status_code
        == 401
    )


def test_provider_lifecycle_contract_reaches_provider_neutral_use_case() -> None:
    client, _bridge, lifecycle = _client()

    response = client.post(
        "/api/provider/v1/channels/lifecycle",
        headers={"Authorization": "Bearer provider-contract-secret"},
        json={
            "operation": "channel.lifecycle",
            "channel_id": "channel-1",
            "device_id": "device-1",
            "state": "active",
            "occurred_at_ms": 1_799_999_000_000,
            "reason": "",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert lifecycle.values[0].state.value == "active"
    assert lifecycle.values[0].device_id == "device-1"
