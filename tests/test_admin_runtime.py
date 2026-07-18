from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_sdk.biz.body import CapabilityManifest
from eidolon_sdk.biz.contracts import CONTROL_TOPIC

from hub.config import AppConfig, LiveKitConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.runtime_blackboard import OwnerRuntimeBlackboard


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


class _FakeDeviceRepo:
    async def get_device(self, device_id: str):
        if device_id != "esp32-1":
            return None
        return SimpleNamespace(
            owner_id="owner-1",
            bound_companion_id="companion-1",
        )


class _FakeDataStore:
    devices = _FakeDeviceRepo()


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
async def test_probe_cycle_activates_transport_with_stale_registration_metadata():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    blackboard = OwnerRuntimeBlackboard()
    await blackboard.register_device_manifest(
        device_id="esp32-1",
        manifest=CapabilityManifest.model_validate(
            {
                "capabilities": [
                    {
                        "name": "camera.capture",
                        "version": 1,
                        "description": "Capture an image.",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "result_schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        ),
        owner_id="owner-1",
        provider_companion_id="companion-1",
        name="Camera",
        registration_id="reg-1",
    )
    runtime = LiveKitAdminRuntime(cfg, data_store=_FakeDataStore(), runtime_blackboard=blackboard)
    fake_api = _FakeLiveKitAPI()
    fake_api.room._participants["room-a"][0].metadata = json.dumps(
        {"kind": "device_control", "registration_id": "stale-transport-generation"}
    )
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])

    entry = await blackboard.get_device(owner_id="owner-1", device_id="esp32-1")
    assert entry is not None
    assert entry.status == "online"
    assert entry.participant_sid == "PA_1"


@pytest.mark.asyncio
async def test_probe_cycle_activates_voice_session_transport():
    """A Box remains capability-online after switching control -> voice room."""
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    blackboard = OwnerRuntimeBlackboard()
    await blackboard.register_device_manifest(
        device_id="esp32-1",
        manifest=CapabilityManifest.model_validate(
            {
                "capabilities": [
                    {
                        "name": "device.identify",
                        "version": 1,
                        "description": "Identify locally.",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "result_schema": {
                            "type": "object",
                            "properties": {"played": {"type": "boolean"}},
                            "required": ["played"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        ),
        owner_id="owner-1",
        provider_companion_id="companion-1",
        name="Box",
        registration_id="reg-voice",
    )
    runtime = LiveKitAdminRuntime(cfg, data_store=_FakeDataStore(), runtime_blackboard=blackboard)
    fake_api = _FakeLiveKitAPI()
    fake_api.room._participants["room-a"][0].metadata = json.dumps(
        {"kind": "device", "registration_id": "reg-voice"}
    )
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])

    entry = await blackboard.get_device(owner_id="owner-1", device_id="esp32-1")
    assert entry is not None
    assert entry.status == "online"
    assert entry.room_name == "room-a"
    command = await runtime.send_command("esp32-1", {}, op="device.identify")
    assert command["status"] == "sent"
    assert fake_api.room.sent_payloads[0].room == "room-a"


@pytest.mark.asyncio
async def test_probe_cycle_requests_signed_reregistration_when_manifest_is_missing():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    blackboard = OwnerRuntimeBlackboard()
    runtime = LiveKitAdminRuntime(cfg, data_store=_FakeDataStore(), runtime_blackboard=blackboard)
    fake_api = _FakeLiveKitAPI()
    fake_api.room._participants["room-a"][0].metadata = json.dumps(
        {
            "kind": "device_control",
            "registration_id": "reg-from-before-hub-restart",
        }
    )
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    await runtime.run_probe_cycle(["esp32-1"])

    assert len(fake_api.room.sent_payloads) == 1
    envelope = json.loads(fake_api.room.sent_payloads[0].data.decode("utf-8"))
    assert envelope["op"] == "config.refresh"
    assert envelope["payload"] == {"reason": "runtime_blackboard_manifest_missing"}
    assert envelope["qos"] == "fire_and_forget"


@pytest.mark.asyncio
async def test_probe_cycle_does_not_require_registration_id_when_manifest_exists():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    blackboard = OwnerRuntimeBlackboard()
    await blackboard.register_device_manifest(
        device_id="esp32-1",
        manifest=CapabilityManifest.model_validate(
            {
                "capabilities": [
                    {
                        "name": "device.identify",
                        "version": 1,
                        "description": "Identify locally.",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "result_schema": {
                            "type": "object",
                            "properties": {"played": {"type": "boolean"}},
                            "required": ["played"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        ),
        owner_id="owner-1",
        provider_companion_id="companion-1",
        name="Box",
    )
    runtime = LiveKitAdminRuntime(cfg, data_store=_FakeDataStore(), runtime_blackboard=blackboard)
    fake_api = _FakeLiveKitAPI()
    fake_api.room._participants["room-a"][0].metadata = json.dumps({"kind": "guard_control"})
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])

    entry = await blackboard.get_device(owner_id="owner-1", device_id="esp32-1")
    assert entry is not None
    assert entry.status == "online"
    assert fake_api.room.sent_payloads == []


@pytest.mark.asyncio
async def test_probe_prefers_control_session_over_same_device_voice_session():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    fake_api.room._rooms = [
        SimpleNamespace(name="voice-room"),
        SimpleNamespace(name="control-room"),
    ]
    fake_api.room._participants = {
        "voice-room": [
            SimpleNamespace(
                identity="esp32-1",
                sid="PA_VOICE",
                metadata=json.dumps({"kind": "device"}),
            )
        ],
        "control-room": [
            SimpleNamespace(
                identity="esp32-1",
                sid="PA_CONTROL",
                metadata=json.dumps({"kind": "device_control"}),
            )
        ],
    }
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command("esp32-1", {}, op="device.identify")

    assert command["status"] == "sent"
    assert fake_api.room.sent_payloads[0].room == "control-room"


@pytest.mark.asyncio
async def test_event_subscriber_keeps_latest_events_when_queue_is_full():
    cfg = AppConfig()
    runtime = LiveKitAdminRuntime(cfg)
    queue = await runtime.subscribe()

    try:
        for i in range(105):
            await runtime._emit_event({"type": "synthetic", "seq": i})

        assert queue.qsize() == 100
        first = json.loads(queue.get_nowait())
        assert first["seq"] == 5
    finally:
        runtime.unsubscribe(queue)


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
async def test_probe_client_initialization_failure_emits_probe_error_event():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="not-a-management-url", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    queue = await runtime.subscribe()

    try:
        await runtime.run_probe_cycle(["esp32-1"])
        event = json.loads(queue.get_nowait())
        assert event["type"] == "probe_error"
        assert "http(s):// URL" in event["error"]
        assert event["known"] == 1
        assert event["consecutive_failures"] == 1
        assert runtime.get_probe_health().consecutive_failures == 1
    finally:
        runtime.unsubscribe(queue)


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

    stored = await runtime.get_command(command["command_id"])
    assert stored is not None
    assert stored["status"] == "timeout"

    commands = await runtime.list_commands(limit=10)
    assert commands
    assert commands[0]["command_id"] == command["command_id"]

    metrics = await runtime.get_metrics()
    assert metrics["commands"]["timeout"] == 1


@pytest.mark.asyncio
async def test_accepted_command_uses_ttl_for_result_timeout():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command(
        "esp32-1",
        {"profile_revision": 1},
        op="guard.owner_face_profile.sync",
        ttl_ms=300_000,
        qos="result",
    )
    accepted = await runtime.apply_command_ack(
        {
            "v": 1,
            "kind": "ack",
            "ref": command["command_id"],
            "device_id": "esp32-1",
            "op": "guard.owner_face_profile.sync",
            "status": "accepted",
            "code": "OK",
        }
    )

    assert accepted is not None
    assert accepted["status"] == "accepted"
    assert await runtime.mark_command_timeout(timeout_seconds=0) == 0


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
async def test_capability_result_schema_failure_turns_completed_result_into_failure():
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

    await runtime.run_probe_cycle(["esp32-1"])
    command = await runtime.send_command(
        "esp32-1",
        {},
        op="device.roll_call",
        capability_version=1,
        capability_contract={
            "name": "device.roll_call",
            "version": 1,
            "manifest_revision": "sha256:test",
            "result_schema": {
                "type": "object",
                "properties": {"played": {"type": "boolean"}},
                "required": ["played"],
                "additionalProperties": False,
            },
        },
        qos="result",
    )

    updated = await runtime.apply_command_ack(
        {
            "v": 1,
            "kind": "result",
            "ref": command["command_id"],
            "device_id": "esp32-1",
            "op": "device.roll_call",
            "capability_version": 1,
            "status": "completed",
            "code": "OK",
            "result": {"played": "yes"},
        }
    )

    assert updated is not None
    assert updated["status"] == "failed"
    assert "invalid capability result" in updated["error"]
    assert "$.played expected boolean" in updated["error"]
    assert updated["result"] == {"played": "yes"}


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
    stored = await runtime.get_command(command["command_id"])
    assert stored["status"] == "sent"


@pytest.mark.asyncio
async def test_send_command_persists_body_command_status(tmp_path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        owner = await store.owners.create(owner_id="owner-1", display_name="Owner")
        companion = await store.companions.create(
            companion_id="companion-1",
            owner_id=owner.owner_id,
            display_name="Xiaoyi",
        )
        await store.devices.create_device(
            device_id="esp32-1",
            owner_id=owner.owner_id,
            kind="esp32",
            bound_companion_id=companion.companion_id,
        )

        cfg = AppConfig()
        cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
        runtime = LiveKitAdminRuntime(cfg, data_store=store)
        fake_api = _FakeLiveKitAPI()
        runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]

        await runtime.run_probe_cycle(["esp32-1"])
        command = await runtime.send_command(
            "esp32-1",
            {},
            op="device.identify",
            runtime_caller_id="rc-1",
            runtime_session_id="rs-1",
            runtime_trace_id="trace-1",
            runtime_turn_id="turn-1",
            runtime_tool_call_id="call-1",
            idempotency_key="idem-1",
            source_device_id="source-1",
            capability_version=1,
            capability_contract={
                "name": "device.identify",
                "version": 1,
                "manifest_revision": "sha256:test",
                "result_schema": {
                    "type": "object",
                    "properties": {"played": {"type": "boolean"}},
                },
            },
        )
        row = await store.body_commands.get_command(command["command_id"])

        assert row is not None
        assert row.status == "sent"
        assert row.owner_id == owner.owner_id
        assert row.companion_id == companion.companion_id
        assert row.device_id == "esp32-1"
        assert row.runtime_caller_id == "rc-1"
        assert row.runtime_session_id == "rs-1"
        assert row.source_device_id == "source-1"
        assert row.envelope_json["_hub_runtime"]["trace_id"] == "trace-1"
        assert row.envelope_json["_hub_capability_contract"]["version"] == 1

        await runtime.apply_command_ack(
            {
                "v": 1,
                "kind": "ack",
                "ref": command["command_id"],
                "device_id": "esp32-1",
                "op": "device.identify",
                "status": "accepted",
                "code": "OK",
            }
        )
        persisted = await store.body_commands.get_command(command["command_id"])
        assert persisted is not None
        assert persisted.status == "accepted"
        assert persisted.ack_json["code"] == "OK"

        cold_runtime = LiveKitAdminRuntime(cfg, data_store=store)
        cold_command = await cold_runtime.get_command(command["command_id"])
        assert cold_command is not None
        assert cold_command["status"] == "accepted"
        assert cold_command["runtime_caller_id"] == "rc-1"
        assert cold_command["runtime_session_id"] == "rs-1"
        assert cold_command["runtime_trace_id"] == "trace-1"
        assert cold_command["runtime_turn_id"] == "turn-1"
        assert cold_command["runtime_tool_call_id"] == "call-1"
        assert cold_command["idempotency_key"] == "idem-1"
        assert cold_command["capability_version"] == 1
        assert "_hub_runtime" not in cold_command["envelope"]
        assert cold_command["source_device_id"] == "source-1"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_command_terminal_results_emit_audit_events(tmp_path):
    """P3 (hub) — terminal command results write device.command.* audit events."""
    from eidolon_data.testing import assert_event

    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        owner = await store.owners.create(owner_id="owner-cmd", display_name="Owner")
        companion = await store.companions.create(
            companion_id="companion-cmd", owner_id=owner.owner_id, display_name="Yi"
        )
        await store.devices.create_device(
            device_id="esp32-1",
            owner_id=owner.owner_id,
            kind="esp32",
            bound_companion_id=companion.companion_id,
        )

        cfg = AppConfig()
        cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
        runtime = LiveKitAdminRuntime(cfg, data_store=store)
        runtime._build_livekit_api = lambda: _FakeLiveKitAPI()  # type: ignore[method-assign]
        await runtime.run_probe_cycle(["esp32-1"])

        # success ack → device.command.acked
        c1 = await runtime.send_command("esp32-1", {"reason": "t"}, op="config.refresh")
        await runtime.apply_command_ack(
            {
                "v": 1,
                "kind": "result",
                "ref": c1["command_id"],
                "device_id": "esp32-1",
                "op": "config.refresh",
                "status": "completed",
                "code": "OK",
            }
        )
        # timeout → device.command.failed (hub-detected)
        c2 = await runtime.send_command("esp32-1", {"reason": "t2"}, op="config.refresh")
        await runtime.mark_command_timeout(timeout_seconds=0)

        events = await store.events.list_for_subject(subject_type="device", subject_id="esp32-1")
        acked = assert_event(events, event_type="device.command.acked")
        assert acked.source == "hub"
        assert acked.owner_id == "owner-cmd"
        assert acked.companion_id == "companion-cmd"
        assert acked.payload_json["command_id"] == c1["command_id"]

        failed = assert_event(events, event_type="device.command.failed")
        assert failed.severity == "warn"  # catalog default for device.command.failed
        assert failed.outcome == "failure"
        assert failed.payload_json["command_id"] == c2["command_id"]
    finally:
        await store.close()
