"""The internal HTTP surface for receipts, changes and accounts."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hub.smarthome.http import create_smarthome_router
from hub.smarthome.providers.virtual import VirtualProvider
from hub.smarthome.runtime import SmartHomeRuntime

from .helpers import OWNER, Clock, FakeRegistry, home

TOKEN = "t" * 40


@pytest.fixture
async def client(tmp_path):
    provider = VirtualProvider(tmp_path / "virtual.sqlite3")
    provider.initialize()
    runtime = SmartHomeRuntime(
        registry=FakeRegistry({OWNER: home()}), providers={"virtual": provider}, now_ms=Clock()
    )
    app = FastAPI()
    app.include_router(create_smarthome_router(lambda: runtime, lambda: TOKEN))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub") as http:
        yield http


async def post(client, path, **body):
    return await client.post(
        f"/api/smarthome/v1/{path}",
        json={"owner_id": OWNER, **body},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def test_execute_then_receipt_and_changes(client):
    request = {
        "request_id": "r1",
        "commands": [
            {"device_id": "living.main_light", "trait": "on_off", "command": "on", "params": {}}
        ],
        "origin": {"kind": "text"},
        "deadline_ms": 1_700_000_003_000,
    }
    result = await post(client, "execute", request=request)
    assert result.status_code == 200 and result.json()["results"][0]["status"] == "succeeded"
    receipt = await post(client, "receipts", request_id="r1")
    assert receipt.status_code == 200
    body = receipt.json()
    assert body["result"]["results"][0]["status"] == "succeeded"
    assert set(body["timestamps"]) == {
        "submitted_at_ms",
        "provider_started_at_ms",
        "provider_returned_at_ms",
        "confirmed_at_ms",
        "completed_at_ms",
        "reconciled_at_ms",
    }
    assert (await post(client, "receipts", request_id="nope")).status_code == 404
    changes = await post(client, "changes", since=0)
    assert changes.status_code == 200 and changes.json()["seq"] >= 1
    assert any(
        c["device_id"] == "living.main_light" and c["state"]["on"]
        for c in changes.json()["changes"]
    )
    later = await post(client, "changes", since=changes.json()["seq"], timeout_ms=10)
    assert later.json()["changes"] == []


async def test_accounts_are_503_when_no_account_provider_is_configured(client):
    assert (await post(client, "providers")).status_code == 503
    assert (await post(client, "accounts")).status_code == 503


async def test_bad_token_is_401(client):
    response = await client.post(
        "/api/smarthome/v1/snapshot",
        json={"owner_id": OWNER},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401
