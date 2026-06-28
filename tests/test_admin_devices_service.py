from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_data.adapters import EidolonDataDeviceRegistryRepository

from hub.api.routers.admin.service import build_admin_devices, refresh_admin_devices
from hub.core.admin_runtime import DevicePresence
from hub.core.device_manager import DeviceManager


class _RuntimeWithPresence:
    async def get_presence_snapshot(self):
        return [
            DevicePresence(
                device_id="esp32-1",
                status="online",
                room_name="device-esp32-1",
                participant_sid="PA_ESP32",
                last_seen_at=datetime(2026, 6, 13, tzinfo=UTC),
            ),
            DevicePresence(
                device_id="agent-AJ_noise",
                status="online",
                room_name="agent-room",
                participant_sid="PA_AGENT",
                last_seen_at=datetime(2026, 6, 13, tzinfo=UTC),
            ),
        ]


class _RefreshRuntime(_RuntimeWithPresence):
    def __init__(self):
        self.probed_device_ids = None

    async def run_probe_cycle(self, known_device_ids):
        self.probed_device_ids = list(known_device_ids)

    async def get_presence_snapshot(self):
        return [
            DevicePresence(
                device_id="esp32-1",
                status="online",
                room_name="device-esp32-1",
                participant_sid="PA_ESP32",
                last_seen_at=datetime(2026, 6, 13, tzinfo=UTC),
            )
        ]


class _ControlBridge:
    def __init__(self):
        self.synced_rooms = None

    async def sync_rooms(self, room_names):
        self.synced_rooms = list(room_names)


@pytest.mark.asyncio
async def test_admin_devices_only_lists_registered_devices(tmp_path: Path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    manager = DeviceManager(EidolonDataDeviceRegistryRepository(store, owner_id="owner-test"))
    await manager.load()
    await manager.register_seen(
        device_id="esp32-1",
        name="Touch AMOLED",
        metadata={"kind": "esp32"},
    )

    rows = await build_admin_devices(
        runtime=_RuntimeWithPresence(),  # type: ignore[arg-type]
        device_manager=manager,
    )

    assert [row.device_id for row in rows] == ["esp32-1"]
    assert rows[0].kind == "esp32"
    assert rows[0].status == "online"
    assert rows[0].room_name == "device-esp32-1"
    await store.close()


@pytest.mark.asyncio
async def test_refresh_admin_devices_runs_probe_then_returns_registered_devices(tmp_path: Path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    manager = DeviceManager(EidolonDataDeviceRegistryRepository(store, owner_id="owner-test"))
    await manager.load()
    await manager.register_seen(
        device_id="esp32-1",
        name="Touch AMOLED",
        metadata={"kind": "esp32"},
    )
    runtime = _RefreshRuntime()
    bridge = _ControlBridge()

    rows = await refresh_admin_devices(
        runtime=runtime,  # type: ignore[arg-type]
        device_manager=manager,
        control_bridge=bridge,
        command_timeout_seconds=None,
    )

    assert runtime.probed_device_ids == ["esp32-1"]
    assert bridge.synced_rooms == ["device-esp32-1"]
    assert [row.device_id for row in rows] == ["esp32-1"]
    assert rows[0].participant_sid == "PA_ESP32"
    await store.close()
