from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eidolon_sdk.adapters.registry_sqlite import DeviceRepository, RegistrySqliteStore
from hub.api.routers.admin.service import build_admin_devices
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


@pytest.mark.asyncio
async def test_admin_devices_only_lists_registered_devices(tmp_path: Path):
    store = RegistrySqliteStore(tmp_path / "registry.sqlite3")
    manager = DeviceManager(DeviceRepository(store))
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
    await store.dispose()
