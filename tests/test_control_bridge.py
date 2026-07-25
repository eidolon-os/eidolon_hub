from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.contracts import (
    CONTROL_TOPIC,
    EVENT_TOPIC,
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


@pytest.mark.asyncio
async def test_control_bridge_forwards_terminal_guard_runtime_result_to_reconciler():
    class RuntimeReconciler:
        def __init__(self) -> None:
            self.commands = []

        async def apply_command_result(self, command):
            self.commands.append(command)

    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]
    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command(
        "esp32-1",
        {"binding_id": "gb-1", "runtime_revision": 1, "desired_runtime_state": "running"},
        op="guard.runtime.sync",
        qos="result",
    )
    reconciler = RuntimeReconciler()
    bridge = LiveKitControlBridge(cfg, runtime, guard_runtime_reconciler=reconciler)
    packet = SimpleNamespace(
        topic=CONTROL_TOPIC,
        data=json.dumps(
            {
                "v": 1,
                "kind": "result",
                "ref": command["command_id"],
                "device_id": "esp32-1",
                "op": "guard.runtime.sync",
                "status": "completed",
                "code": "OK",
                "result": {
                    "binding_id": "gb-1",
                    "runtime_revision": 1,
                    "desired_runtime_state": "running",
                    "running": True,
                },
            }
        ).encode("utf-8"),
        participant=SimpleNamespace(identity="esp32-1"),
    )

    await bridge._handle_packet(packet)

    assert len(reconciler.commands) == 1
    assert reconciler.commands[0]["status"] == "succeeded"


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


@pytest.mark.asyncio
async def test_control_bridge_routes_event_topic_to_ambient_event_bus():
    class EventBus:
        def __init__(self):
            self.calls = []

        async def handle_packet(self, **kwargs):
            self.calls.append(kwargs)

    cfg = AppConfig()
    runtime = LiveKitAdminRuntime(cfg)
    event_bus = EventBus()
    bridge = LiveKitControlBridge(
        cfg,
        runtime,
        ambient_event_bus=event_bus,  # type: ignore[arg-type]
    )
    packet = SimpleNamespace(
        topic=EVENT_TOPIC,
        data=b'{"kind":"event"}',
        participant=SimpleNamespace(identity="box3-1"),
    )

    await bridge._handle_packet(packet)

    assert event_bus.calls == [
        {
            "topic": EVENT_TOPIC,
            "data": b'{"kind":"event"}',
            "sender_identity": "box3-1",
        }
    ]


@pytest.mark.asyncio
async def test_control_bridge_routes_guard_facts_from_eidolon_control():
    class GuardFixture:
        def __init__(self):
            self.calls = []

        async def handle(self, payload, *, sender_identity="", source="fixture"):
            self.calls.append((payload, sender_identity, source))

    cfg = AppConfig()
    runtime = LiveKitAdminRuntime(cfg)
    fixture = GuardFixture()
    bridge = LiveKitControlBridge(cfg, runtime, guard_control_plane=fixture)  # type: ignore[arg-type]
    payload = {
        "type": "guard.presence.candidate",
        "schema_v": 1,
        "guard_companion_id": "guard-1",
        "device_id": "atk-1",
        "correlation_id": "corr-1",
        "guard_epoch": 1,
        "ts_ms": 1,
        "signals": {"motion_cells": 1},
        "raw_retention": "none",
        "debounce_ms": 500,
    }
    packet = SimpleNamespace(
        topic=CONTROL_TOPIC,
        data=json.dumps(payload).encode("utf-8"),
        participant=SimpleNamespace(identity="atk-1"),
    )

    await bridge._handle_packet(packet)

    assert fixture.calls == [(payload, "atk-1", "livekit")]
