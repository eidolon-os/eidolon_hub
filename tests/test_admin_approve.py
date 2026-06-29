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
from eidolon_data import DataSettings, DataStore
from eidolon_data.adapters import EidolonDataDeviceRegistryRepository
from fastapi.testclient import TestClient

from hub.config import AppConfig
from hub.core.device_manager import DeviceManager
from hub.main import create_app


class _NoopAdminRuntime:
    """Stand-in for ``LiveKitAdminRuntime``.

    The list / detail endpoints read presence from the runtime, and approve
    sends a best-effort post-approval config.refresh when the device is online.
    This stub records the refresh without requiring a real LiveKit URL.
    """

    def __init__(self):
        self.commands = []

    async def get_presence_snapshot(self):
        return []

    def get_probe_health(self):
        return SimpleNamespace(
            running=False, last_success_at=None, last_error="",
            consecutive_failures=0, total_cycles=0,
        )

    async def send_command(self, device_id: str, payload: dict, **kwargs):
        command = {"device_id": device_id, "payload": payload, **kwargs}
        self.commands.append(command)
        return {"command_id": "cmd-refresh", **command}


def _new_device_manager(db_path: Path) -> tuple[DeviceManager, DataStore]:
    import asyncio

    store = DataStore.open(DataSettings(sqlite_path=str(db_path)))
    manager = DeviceManager(EidolonDataDeviceRegistryRepository(store, owner_id="owner-test"))
    asyncio.run(manager.load())
    return manager, store


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Real FastAPI app, real DeviceManager backed by a tmp_path SQLite DB,
    stubbed admin_runtime (LiveKit-free)."""
    import asyncio

    registry_db = tmp_path / "eidolon.sqlite3"
    cfg = AppConfig()
    app = create_app(cfg)

    dm, store = _new_device_manager(registry_db)
    app.state.device_manager = dm
    app.state.admin_runtime = _NoopAdminRuntime()

    # Seed one device so approve has something to act on. Direct use of the
    # manager mirrors how production registers devices (no public HTTP
    # registration endpoint exists yet).
    asyncio.run(dm.register_seen(device_id="dev-001", name="Test Device"))

    yield TestClient(app)
    asyncio.run(store.close())


# ---- happy path -----------------------------------------------------------


def test_approve_returns_200_with_new_state(client: TestClient) -> None:
    resp = client.post("/api/admin/devices/dev-001/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "dev-001"
    assert body["approved"] is True
    assert body["approved_at"] is not None


def test_approve_sends_best_effort_config_refresh(client: TestClient) -> None:
    resp = client.post("/api/admin/devices/dev-001/approve")
    assert resp.status_code == 200

    commands = client.app.state.admin_runtime.commands
    assert commands == [
        {
            "device_id": "dev-001",
            "payload": {"reason": "device_approved"},
            "op": "config.refresh",
            "ttl_ms": 30_000,
            "qos": "ack",
            "priority": "high",
        }
    ]


def test_approve_still_succeeds_when_post_approval_refresh_cannot_send(
    client: TestClient,
) -> None:
    async def _offline(*_args, **_kwargs):
        raise ValueError("Device dev-001 is not currently connected")

    client.app.state.admin_runtime.send_command = _offline
    resp = client.post("/api/admin/devices/dev-001/approve")

    assert resp.status_code == 200
    assert resp.json()["approved"] is True


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


def test_set_enabled_persists_to_eidolon_data(tmp_path: Path) -> None:
    import asyncio

    registry_db = tmp_path / "eidolon.sqlite3"
    dm, store = _new_device_manager(registry_db)
    asyncio.run(dm.register_seen(device_id="dev-001", name="Test Device"))
    asyncio.run(dm.set_enabled("dev-001", enabled=False))
    asyncio.run(store.close())

    reloaded, reloaded_store = _new_device_manager(registry_db)
    device = reloaded.get("dev-001")
    assert device is not None
    assert device.enabled is False
    asyncio.run(reloaded_store.close())


def test_set_enabled_unknown_device_returns_404(client: TestClient) -> None:
    resp = client.post("/api/admin/devices/ghost/enable", params={"enabled": False})
    assert resp.status_code == 404
    assert "Device not found" in resp.json()["detail"]


def test_device_manager_does_not_create_devices_json(tmp_path: Path) -> None:
    import asyncio

    dm, store = _new_device_manager(tmp_path / "eidolon.sqlite3")
    asyncio.run(dm.register_seen(device_id="dev-001", name="Test Device"))
    asyncio.run(dm.set_enabled("dev-001", enabled=False))

    assert not (tmp_path / "devices.json").exists()
    asyncio.run(store.close())
