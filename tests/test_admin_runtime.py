from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.contracts import CONTROL_TOPIC

from hub.config import AppConfig, LiveKitConfig
from hub.core.admin_runtime import LiveKitAdminRuntime


class _FakeRoomService:
    def __init__(self):
        self._rooms = [SimpleNamespace(name="room-a")]
        self._participants = {
            "room-a": [
                SimpleNamespace(identity="esp32-1", sid="PA_1"),
                SimpleNamespace(identity="agent-AJ_noise", sid="PA_AGENT"),
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


class _FakeControlBridge:
    def __init__(self):
        self.rooms = []

    async def ensure_room(self, room_name: str):
        self.rooms.append(room_name)


@pytest.mark.asyncio
async def test_probe_cycle_updates_presence():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
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
async def test_probe_cycle_ignores_unregistered_livekit_participants():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle([])
    devices = await runtime.get_presence_snapshot()
    assert devices == []


@pytest.mark.asyncio
async def test_send_command_to_online_device():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]
    bridge = _FakeControlBridge()
    runtime.set_control_bridge(bridge)

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"reason": "test"}, op="config.refresh")

    assert command["status"] == "sent"
    assert command["device_id"] == "esp32-1"
    assert command["topic"] == CONTROL_TOPIC
    assert command["op"] == "config.refresh"
    assert fake_api.room.sent_payloads
    sent = fake_api.room.sent_payloads[0]
    assert sent.room == "room-a"
    assert sent.topic == CONTROL_TOPIC
    assert "esp32-1" in sent.destination_identities
    assert bridge.rooms == ["room-a"]
    envelope = json.loads(sent.data.decode("utf-8"))
    assert envelope["v"] == 1
    assert envelope["kind"] == "cmd"
    assert envelope["id"] == command["command_id"]
    assert envelope["op"] == "config.refresh"
    assert envelope["dst"]["id"] == "esp32-1"
    assert envelope["payload"] == {"reason": "test"}


@pytest.mark.asyncio
async def test_send_command_normalizes_legacy_ui_aliases():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {}, op="identify")

    assert command["op"] == "device.identify"
    sent = fake_api.room.sent_payloads[0]
    envelope = json.loads(sent.data.decode("utf-8"))
    assert envelope["op"] == "device.identify"


@pytest.mark.asyncio
async def test_probe_failure_marks_unknown():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    runtime._build_livekit_api = lambda: _FailingLiveKitAPI()  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    devices = await runtime.get_presence_snapshot()
    assert devices[0].status == "unknown"
    assert runtime.get_probe_health().consecutive_failures == 1


@pytest.mark.asyncio
async def test_offline_probe_clears_stale_room_and_blocks_commands():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    cfg.admin.offline_after_missed_probes = 1
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    fake_api.room._participants = {"room-a": []}
    await runtime.run_probe_cycle(["esp32-1"])

    devices = await runtime.get_presence_snapshot()
    assert devices[0].status == "offline"
    assert devices[0].room_name == ""
    assert devices[0].participant_sid == ""
    with pytest.raises(ValueError, match="not currently connected"):
        await runtime.send_command("esp32-1", {"reason": "test"}, op="device.identify")


@pytest.mark.asyncio
async def test_mark_command_timeout_and_metrics():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
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


@pytest.mark.asyncio
async def test_apply_command_ack_updates_command_status():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"reason": "test"}, op="config.refresh")

    updated = await runtime.apply_command_ack(
        {
            "v": 1,
            "kind": "result",
            "ref": command["command_id"],
            "device_id": "esp32-1",
            "op": "config.refresh",
            "status": "completed",
            "code": "OK",
            "result": {"status": "active"},
        }
    )

    assert updated is not None
    assert updated["status"] == "succeeded"
    assert updated["error"] == ""
    assert updated["ack"]["code"] == "OK"
    assert updated["result"] == {"status": "active"}


@pytest.mark.asyncio
async def test_apply_command_ack_rejects_wrong_sender():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"reason": "test"}, op="config.refresh")

    updated = await runtime.apply_command_ack(
        {
            "v": 1,
            "kind": "ack",
            "ref": command["command_id"],
            "device_id": "esp32-1",
            "op": "config.refresh",
            "status": "succeeded",
            "code": "OK",
        },
        sender_identity="esp32-other",
    )

    assert updated is None
    assert runtime.get_command(command["command_id"])["status"] == "sent"
