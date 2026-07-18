from __future__ import annotations

import pytest
from eidolon_data import DataSettings, DataStore
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hub.api.routers.system.sense import router as sense_router
from hub.core.sense_ingress import SenseIngress, SenseIngressError


@pytest.fixture
async def store(tmp_path):
    s = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "sense.sqlite3")))
    await s.init_schema()
    await s.owners.create(owner_id="owner-1", display_name="Owner")
    await s.devices.create_device(device_id="atk-1", owner_id="owner-1", capabilities_json={})
    try:
        yield s
    finally:
        await s.close()


def _fatigue(**over) -> dict:
    return {
        "type": "sense.fatigue",
        "schema_v": 1,
        "owner_id": "owner-1",
        "device_id": "atk-1",
        "correlation_id": "sn-1",
        "epoch": 1,
        "ts_ms": 1_700_000_000_000,
        "hint": "yawn",
        "model_id": "mediapipe-face-landmarker",
        "model_version": "mar-ear-v1",
        "signals": {"yawn_count": 3},
        "raw_retention": "none",
        **over,
    }


def _attention(**over) -> dict:
    return {
        "type": "sense.attention",
        "schema_v": 1,
        "owner_id": "owner-1",
        "device_id": "atk-1",
        "correlation_id": "sn-a",
        "epoch": 1,
        "ts_ms": 1_700_000_000_000,
        "state": "focused",
        "raw_retention": "none",
        **over,
    }


async def test_fatigue_fact_accepted_and_audited(store) -> None:
    accepted = await SenseIngress(store).ingest(_fatigue())
    assert accepted["type"] == "sense.fatigue"
    assert accepted["hint"] == "yawn"
    assert accepted["owner_id"] == "owner-1"


async def test_owner_mismatch_rejected(store) -> None:
    # device atk-1 belongs to owner-1; a fact claiming owner-2 is rejected
    with pytest.raises(SenseIngressError):
        await SenseIngress(store).ingest(_fatigue(owner_id="owner-2"))


async def test_unknown_device_rejected(store) -> None:
    with pytest.raises(SenseIngressError):
        await SenseIngress(store).ingest(_fatigue(device_id="atk-unknown"))


async def test_vision_worker_may_not_emit_attention(store) -> None:
    # attention/session are device-origin (T0); the worker source is bounded.
    with pytest.raises(SenseIngressError):
        await SenseIngress(store).ingest(_attention())


async def test_rejects_sensitive_or_unknown_field(store) -> None:
    with pytest.raises(ValueError):
        await SenseIngress(store).ingest(_fatigue(image="aGVsbG8="))


async def test_http_endpoint_accepts_and_rejects(store) -> None:
    app = FastAPI()
    app.state.data_store = store
    app.include_router(sense_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/internal/sense/facts", json=_fatigue())
        assert ok.status_code == 202
        assert ok.json()["accepted"]["type"] == "sense.fatigue"

        conflict = await client.post("/internal/sense/facts", json=_fatigue(owner_id="owner-2"))
        assert conflict.status_code == 409

        bad = await client.post("/internal/sense/facts", json=_fatigue(image="x"))
        assert bad.status_code == 422
