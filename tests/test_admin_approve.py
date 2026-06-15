"""End-to-end tests for POST /api/admin/devices/{id}/approve.

This is the only endpoint admin.eidolon_admin's orchestrator calls when
the operator clicks 【批准】. The contract these tests lock:

- 200 on first approve, with the new approved + approved_at fields
- idempotent: a second call doesn't bump approved_at
- 404 for an unknown device (admin's orchestrator depends on this to
  give the operator a friendly "device not registered yet" message)
- the state actually persists in the DeviceManager (not just echoed
  in the response)

We use ``fastapi.testclient.TestClient`` against the real ``create_app``,
wiring the device_manager + a no-op admin_runtime stub directly into
``app.state`` so we don't have to spin up LiveKit just to test approve.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from hub.config import AppConfig
from hub.core.device_manager import DeviceManager
from hub.main import create_app


class _NoopAdminRuntime:
    """Stand-in for ``LiveKitAdminRuntime``.

    The approve endpoint never touches the runtime, but the list / detail
    endpoints do — providing a minimal stub keeps the app importable
    without a real LiveKit URL.
    """

    async def get_presence_snapshot(self):
        return []

    def get_probe_health(self):
        return SimpleNamespace(
            running=False, last_success_at=None, last_error="",
            consecutive_failures=0, total_cycles=0,
        )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Real FastAPI app, real DeviceManager backed by a tmp_path JSON file,
    stubbed admin_runtime (LiveKit-free)."""
    devices_file = tmp_path / "devices.json"
    cfg = AppConfig()
    app = create_app(cfg)

    dm = DeviceManager(devices_file)
    # ``load()`` is a no-op when the file is missing — by-design starts empty.
    import asyncio
    asyncio.run(dm.load())
    app.state.device_manager = dm
    app.state.admin_runtime = _NoopAdminRuntime()

    # Seed one device so approve has something to act on. Direct use of the
    # manager mirrors how production registers devices (no public HTTP
    # registration endpoint exists yet).
    dm.register(device_id="dev-001", name="Test Device")

    yield TestClient(app)


# ---- happy path -----------------------------------------------------------


def test_approve_returns_200_with_new_state(client: TestClient) -> None:
    resp = client.post("/api/admin/devices/dev-001/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "dev-001"
    assert body["approved"] is True
    assert body["approved_at"] is not None


def test_approve_persists_state_in_device_manager(client: TestClient) -> None:
    """Approve writes to in-memory state — a subsequent GET reflects it.

    We use the existing /api/admin/devices listing to verify the round-
    trip rather than poking the DeviceManager directly; that's the path
    admin.eidolon_admin would use to read back the change.
    """
    client.post("/api/admin/devices/dev-001/approve")
    listed = client.get("/api/admin/devices").json()
    devices = {d["device_id"]: d for d in listed["devices"]}
    assert devices["dev-001"]["approved"] is True
    assert devices["dev-001"]["approved_at"] is not None


# ---- idempotency ----------------------------------------------------------


def test_approve_is_idempotent_preserving_first_timestamp(client: TestClient) -> None:
    """Calling approve twice must not advance ``approved_at``.

    Operator may click the button twice; the audit timestamp should
    reflect the FIRST decision, not the latest click — otherwise log
    correlation against the supervisor event stream breaks.
    """
    first = client.post("/api/admin/devices/dev-001/approve").json()
    second = client.post("/api/admin/devices/dev-001/approve").json()
    assert first["approved_at"] == second["approved_at"]


# ---- error mapping --------------------------------------------------------


def test_approve_unknown_device_returns_404_with_actionable_message(
    client: TestClient,
) -> None:
    """The 404 detail tells the operator what they actually need to do
    next ("wait for it to call GET /api/config…") — that exact wording
    surfaces in admin UI's error toast."""
    resp = client.post("/api/admin/devices/ghost/approve")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "not registered" in detail
    assert "/api/config" in detail


# ---- enable / disable -----------------------------------------------------


def test_set_enabled_returns_full_device_view(client: TestClient) -> None:
    resp = client.post("/api/admin/devices/dev-001/enable", params={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "dev-001"
    assert body["enabled"] is False

    listed = client.get("/api/admin/devices").json()
    devices = {d["device_id"]: d for d in listed["devices"]}
    assert devices["dev-001"]["enabled"] is False


def test_set_enabled_persists_to_devices_json(tmp_path: Path) -> None:
    import asyncio

    devices_file = tmp_path / "devices.json"
    dm = DeviceManager(devices_file)
    asyncio.run(dm.load())
    dm.register(device_id="dev-001", name="Test Device")
    asyncio.run(dm.set_enabled("dev-001", enabled=False))

    reloaded = DeviceManager(devices_file)
    asyncio.run(reloaded.load())
    device = reloaded.get("dev-001")
    assert device is not None
    assert device.enabled is False


def test_set_enabled_unknown_device_returns_404(client: TestClient) -> None:
    resp = client.post("/api/admin/devices/ghost/enable", params={"enabled": False})
    assert resp.status_code == 404
    assert "Device not found" in resp.json()["detail"]


# ---- backward compatibility with legacy devices.json ---------------------


def test_legacy_paired_device_loads_as_approved(tmp_path: Path) -> None:
    """Phase 25 migration: if devices.json was written before this phase,
    a record with ``paired=true`` but no ``approved`` field auto-upgrades
    to ``approved=true`` on load.

    Without this, every existing paired device would show as "discovered"
    after the upgrade — the admin UI would treat live, working devices
    as fresh-and-pending. The test pins the migration so a regression
    here is immediately obvious.
    """
    import asyncio
    import json

    devices_file = tmp_path / "devices.json"
    devices_file.write_text(json.dumps({
        "version": 1,
        "devices": {
            "legacy-001": {
                "device_id": "legacy-001",
                "name": "Old Paired Device",
                "enabled": True,
                "paired": True,
                "psk_hash": "sha256:fake",
                "created_at": "2024-01-01T00:00:00+00:00",
                "last_seen": "2024-06-01T00:00:00+00:00",
                "metadata": {},
                # ← intentionally NO approved / approved_at
            }
        },
    }))
    dm = DeviceManager(devices_file)
    asyncio.run(dm.load())
    legacy = dm.get("legacy-001")
    assert legacy is not None
    assert legacy.approved is True
    assert legacy.approved_at is not None


def test_signed_legacy_device_loads_as_esp32(tmp_path: Path) -> None:
    """Signed device records created before ``kind`` existed should still
    show as ESP32 in admin. The public key/fingerprint pair is only written
    by the ESP32 signed config flow, so it is a stable migration signal."""
    import asyncio
    import json

    devices_file = tmp_path / "devices.json"
    devices_file.write_text(json.dumps({
        "version": 1,
        "devices": {
            "1c:db:d4:7a:ef:0c": {
                "device_id": "1c:db:d4:7a:ef:0c",
                "name": "",
                "enabled": True,
                "paired": False,
                "approved": True,
                "approved_at": "2026-06-12T11:38:06+00:00",
                "psk_hash": None,
                "created_at": "2026-06-12T11:35:14+00:00",
                "last_seen": "2026-06-12T16:33:38+00:00",
                "metadata": {
                    "public_key": "pub",
                    "fingerprint": "p256:abc",
                },
            }
        },
    }))
    dm = DeviceManager(devices_file)
    asyncio.run(dm.load())
    device = dm.get("1c:db:d4:7a:ef:0c")
    assert device is not None
    assert device.kind == "esp32"
