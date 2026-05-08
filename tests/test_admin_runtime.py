from __future__ import annotations

from types import SimpleNamespace

import pytest

from hub.config import AppConfig, LiveKitConfig
from hub.core.admin_runtime import LiveKitAdminRuntime


class _FakeRoomService:
    def __init__(self):
        self._rooms = [SimpleNamespace(name="room-a")]
        self._participants = {
            "room-a": [
                SimpleNamespace(identity="esp32-1", sid="PA_1"),
            ]
        }
        self.sent_payloads = []

    async def list_rooms(self, _request):
        return SimpleNamespace(rooms=self._rooms)

    async def list_participants(self, request):
        return SimpleNamespace(participants=self._participants.get(request.room, []))

    async def send_data(self, request):
        self.sent_payloads.append(request)
        return SimpleNamespace()


class _FakeLiveKitAPI:
    def __init__(self):
        self.room = _FakeRoomService()

    async def aclose(self):
        return None


class _FailingLiveKitAPI:
    class _FailingRoomService:
        async def list_rooms(self, _request):
            raise RuntimeError("probe failed")

    def __init__(self):
        self.room = self._FailingRoomService()

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_probe_cycle_updates_presence():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    devices = await runtime.get_presence_snapshot()
    assert len(devices) == 1
    assert devices[0].device_id == "esp32-1"
    assert devices[0].status == "online"
    assert devices[0].room_name == "room-a"


@pytest.mark.asyncio
async def test_send_command_to_online_device():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"power": "on"}, "admin.command")

    assert command["status"] == "sent"
    assert command["device_id"] == "esp32-1"
    assert fake_api.room.sent_payloads
    sent = fake_api.room.sent_payloads[0]
    assert sent.room == "room-a"
    assert "esp32-1" in sent.destination_identities


@pytest.mark.asyncio
async def test_probe_failure_marks_unknown():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    runtime._build_livekit_api = lambda: _FailingLiveKitAPI()  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    devices = await runtime.get_presence_snapshot()
    assert devices[0].status == "unknown"
    assert runtime.get_probe_health().consecutive_failures == 1


@pytest.mark.asyncio
async def test_mark_command_timeout_and_metrics():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"mode": "demo"}, "admin.command")
    touched = await runtime.mark_command_timeout(timeout_seconds=0)
    assert touched == 1

    stored = runtime.get_command(command["command_id"])
    assert stored is not None
    assert stored["status"] == "timeout"

    commands = runtime.list_commands(limit=10)
    assert commands
    assert commands[0]["command_id"] == command["command_id"]

    metrics = await runtime.get_metrics()
    assert metrics["commands"]["timeout"] == 1
