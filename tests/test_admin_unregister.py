"""End-to-end tests for DELETE /api/admin/devices/{id}.

Phase 29.B.3: hub side of the universal "device-can-be-removed" surface.
Admin's later phases need this to support cascade delete (operator
removes a device → admin unbinds it from any agent → admin tells hub
to forget it).

Contract pinned here:
- 200 with ``existed=true, presence_cleared=...`` on first delete
- 200 with ``existed=false`` on repeat (idempotent — safe retry)
- the persistent record is gone from the registry DB AND from the
  in-memory DeviceManager (verify via subsequent GET /api/admin/devices)
  - admin_runtime presence cache is cleared if present
  - other devices are untouched
"""
from __future__ import annotations

import asyncio
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


class _FakeAdminRuntime:
    """Stand-in that records ``forget_presence`` calls so we can assert.

    The real runtime tracks LiveKit presence cache; tests don't need
    LiveKit, but DO need to verify the unregister endpoint calls the
    cleanup hook on real records.
    """

    def __init__(self) -> None:
        # device_id → True if "currently in presence cache"
        self._cache: dict[str, bool] = {}
        self.forget_calls: list[tuple[str, str | None]] = []

    def seed_presence(self, device_id: str) -> None:
        self._cache[device_id] = True

    async def get_presence_snapshot(self):
        return []

    def get_probe_health(self):
        return SimpleNamespace(
            running=False, last_success_at=None, last_error="",
            consecutive_failures=0, total_cycles=0,
        )

    async def forget_presence(
        self,
        device_id: str,
        *,
        owner_id: str | None = None,
    ) -> bool:
        self.forget_calls.append((device_id, owner_id))
        return self._cache.pop(device_id, None) is not None


def _new_device_manager(db_path: Path) -> tuple[DeviceManager, DataStore]:
    store = DataStore.open(DataSettings(sqlite_path=str(db_path)))
    manager = DeviceManager(EidolonDataDeviceRegistryRepository(store, owner_id="owner-test"))
    asyncio.run(manager.load())
    return manager, store


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, DeviceManager, _FakeAdminRuntime, Path, DataStore]]:
    registry_db = tmp_path / "eidolon.sqlite3"
    cfg = AppConfig()
    app = create_app(cfg)

    dm, store = _new_device_manager(registry_db)
    rt = _FakeAdminRuntime()
    app.state.device_manager = dm
    app.state.admin_runtime = rt

    # Two devices: 'alpha' (has presence cache), 'beta' (no cache)
    asyncio.run(dm.register_seen(device_id="alpha", name="Living Room"))
    asyncio.run(dm.register_seen(device_id="beta", name="Bedroom"))
    rt.seed_presence("alpha")

    # NOTE: bare TestClient (no ``with``) — matches the existing
    # test_admin_approve fixture. Entering the ``with`` block would
    # trigger app's lifespan and overwrite ``app.state.device_manager``.
    yield TestClient(app), dm, rt, registry_db, store
    asyncio.run(store.close())


def test_unregister_known_device_returns_200(env) -> None:
    client, _dm, _rt, _, _ = env
    r = client.delete("/api/admin/devices/alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "device_id": "alpha",
        "existed": True,
        "presence_cleared": True,
    }


def test_unregister_removes_from_device_manager(env) -> None:
    """After delete, the device is gone from in-memory state AND from
    the registry DB (so a restart wouldn't reload the deleted record).
    """
    client, dm, _, registry_db, store = env
    client.delete("/api/admin/devices/alpha")
    assert "alpha" not in dm
    asyncio.run(store.close())
    # Registry DB must reflect the removal — load a fresh DeviceManager
    # against the same DB and confirm alpha isn't there.
    dm2, store2 = _new_device_manager(registry_db)
    assert "alpha" not in dm2
    assert "beta" in dm2  # untouched
    asyncio.run(store2.close())


def test_unregister_clears_presence_cache(env) -> None:
    """A device with a presence-cache entry has it cleared on unregister —
    otherwise admin UI shows a ghost row until the next probe cycle.
    """
    client, _, rt, _, _ = env
    r = client.delete("/api/admin/devices/alpha")
    assert r.json()["presence_cleared"] is True
    assert "alpha" not in rt._cache


def test_unregister_idempotent_returns_existed_false(env) -> None:
    """Second DELETE on the same id returns 200 with existed=false. Critical
    for admin cascade: when admin retries DELETE after a partial failure,
    the second call must not error.
    """
    client, _, _, _, _ = env
    first = client.delete("/api/admin/devices/alpha").json()
    assert first["existed"] is True
    second = client.delete("/api/admin/devices/alpha").json()
    assert second == {
        "device_id": "alpha",
        "existed": False,
        "presence_cleared": False,
    }


def test_unregister_device_with_no_presence_cache(env) -> None:
    """Device that was never seen by LiveKit probes (no presence entry).
    existed=true (it's in the registry DB), presence_cleared=false."""
    client, _, _, _, _ = env
    r = client.delete("/api/admin/devices/beta")
    body = r.json()
    assert body["existed"] is True
    assert body["presence_cleared"] is False


def test_unregister_does_not_affect_other_devices(env) -> None:
    """Deleting alpha must leave beta intact in both state and disk."""
    client, dm, _, _, _ = env
    client.delete("/api/admin/devices/alpha")
    listed = client.get("/api/admin/devices").json()
    ids = {d["device_id"] for d in listed["devices"]}
    assert ids == {"beta"}
    assert "beta" in dm


def test_unregister_ghost_device_returns_200(env) -> None:
    """Ghost id (never existed). Idempotent → 200, both flags false.

    Distinct from the approve endpoint which 404s on unknown id.
    Rationale: DELETE is desired-state ("make sure this isn't here"),
    so the absence of the device IS the desired state.
    """
    client, _, _, _, _ = env
    r = client.delete("/api/admin/devices/never-existed")
    assert r.status_code == 200
    assert r.json() == {
        "device_id": "never-existed",
        "existed": False,
        "presence_cleared": False,
    }
