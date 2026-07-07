from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.contracts import (
    CONTROL_TOPIC,
    LIVEKIT_AGENT_SESSION_TOPIC,
    LIVEKIT_TRANSCRIPTION_TOPIC,
)

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

    stored = await runtime.get_command(command["command_id"])
    assert stored["status"] == "accepted"
    assert stored["ack"]["code"] == "OK"


class _FakeRoom:
    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.text_stream_handlers = {}
        self.byte_stream_handlers = {}

    def on(self, _event):
        def _deco(fn):
            return fn

        return _deco

    async def connect(self, _url, _token):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    def register_text_stream_handler(self, topic, handler):
        self.text_stream_handlers[topic] = handler

    def register_byte_stream_handler(self, topic, handler):
        self.byte_stream_handlers[topic] = handler


class _FakeStreamReader:
    def __init__(self, topic: str, chunks: list[object]):
        self.info = SimpleNamespace(topic=topic)
        self._chunks = list(chunks)
        self.consumed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        self.consumed += 1
        return self._chunks.pop(0)


class _FakeRtc:
    def __init__(self):
        self.rooms: list[_FakeRoom] = []

    def Room(self):
        room = _FakeRoom()
        self.rooms.append(room)
        return room


@pytest.mark.asyncio
async def test_sync_rooms_reconciles_joins_and_leaves():
    """I3: sync_rooms is reconciling — it joins missing rooms AND leaves rooms no
    longer desired (an offline device's control room is dropped → bridge leaves →
    room reclaimed)."""
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    bridge = LiveKitControlBridge(cfg, runtime)
    bridge._rtc = _FakeRtc()  # type: ignore[attr-defined]
    bridge._started = True

    # Both devices present → join both control rooms.
    await bridge.sync_rooms(["room-a", "room-b"])
    assert set(bridge._rooms.keys()) == {"room-a", "room-b"}
    room_b = bridge._rooms["room-b"]

    # room-b's device goes offline → caller drops it → bridge leaves room-b only.
    await bridge.sync_rooms(["room-a"])
    assert set(bridge._rooms.keys()) == {"room-a"}
    assert room_b.disconnected is True
    assert bridge._rooms["room-a"].disconnected is False


@pytest.mark.asyncio
async def test_control_bridge_registers_and_drains_livekit_framework_streams():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    bridge = LiveKitControlBridge(cfg, runtime)
    room = _FakeRoom()

    bridge._install_handler(room)

    assert LIVEKIT_TRANSCRIPTION_TOPIC in room.text_stream_handlers
    assert LIVEKIT_AGENT_SESSION_TOPIC in room.byte_stream_handlers

    text_reader = _FakeStreamReader(LIVEKIT_TRANSCRIPTION_TOPIC, ["hello", "world"])
    byte_reader = _FakeStreamReader(LIVEKIT_AGENT_SESSION_TOPIC, [b"a", b"b"])

    room.text_stream_handlers[LIVEKIT_TRANSCRIPTION_TOPIC](text_reader, "agent")
    room.byte_stream_handlers[LIVEKIT_AGENT_SESSION_TOPIC](byte_reader, "agent")
    await asyncio.sleep(0)

    assert text_reader.consumed == 2
    assert byte_reader.consumed == 2


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

    stored = await runtime.get_command(command["command_id"])
    assert stored["status"] == "sent"
