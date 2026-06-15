from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from eidolon_sdk.control import CONTROL_TOPIC

from hub.config import AppConfig, LiveKitConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.control_bridge import LiveKitControlBridge


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


@pytest.mark.asyncio
async def test_control_bridge_applies_livekit_ack_packet():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"reason": "test"}, op="config.refresh")
    bridge = LiveKitControlBridge(cfg, runtime)
    packet = SimpleNamespace(
        topic=CONTROL_TOPIC,
        data=json.dumps(
            {
                "v": 1,
                "kind": "ack",
                "ref": command["command_id"],
                "device_id": "esp32-1",
                "op": "config.refresh",
                "status": "accepted",
                "code": "OK",
            }
        ).encode("utf-8"),
        participant=SimpleNamespace(identity="esp32-1"),
    )

    await bridge._handle_packet(packet)

    stored = runtime.get_command(command["command_id"])
    assert stored["status"] == "accepted"
    assert stored["ack"]["code"] == "OK"


@pytest.mark.asyncio
async def test_control_bridge_ignores_non_ack_control_packet():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {"reason": "test"}, op="config.refresh")
    bridge = LiveKitControlBridge(cfg, runtime)
    packet = SimpleNamespace(
        topic=CONTROL_TOPIC,
        data=json.dumps(
            {
                "v": 1,
                "kind": "cmd",
                "id": "not-an-ack",
                "op": "config.refresh",
            }
        ).encode("utf-8"),
        participant=SimpleNamespace(identity="esp32-1"),
    )

    await bridge._handle_packet(packet)

    assert runtime.get_command(command["command_id"])["status"] == "sent"
