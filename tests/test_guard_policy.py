from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET
from eidolon_sdk.biz.contracts import CONTROL_TOPIC
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hub.api.routers.admin.guard import router as guard_router
from hub.config import AppConfig, LiveKitConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.guard_body_delivery import GuardBodyActionDeliveryWorker
from hub.core.guard_fixture_subscriber import MissionControlFixtureSubscriber
from hub.core.guard_ingress import GuardIngress
from hub.core.guard_policy import GuardControlPlane
from hub.core.guard_policy import GuardPolicyError


@pytest.fixture
async def plane(tmp_path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "guard.sqlite3")))
    await store.init_schema()
    await store.owners.create(owner_id="owner-1", display_name="Owner")
    await store.devices.create_device(
        device_id="atk-1",
        owner_id=None,
        capabilities_json={"guard": {"enabled": True, "protocol_versions": [1]}},
    )
    await store.guard_bindings.ensure_guard_companion(
        owner_id="owner-1", companion_id="guard-1"
    )
    await store.guard_bindings.claim(
        owner_id="owner-1",
        device_id="atk-1",
        guard_companion_id="guard-1",
    )
    try:
        yield GuardControlPlane(store), store
    finally:
        await store.close()


def _candidate() -> dict:
    return {
        "type": "guard.presence.candidate",
        "schema_v": 1,
        "guard_companion_id": "guard-1",
        "device_id": "atk-1",
        "correlation_id": "corr-1",
        "guard_epoch": 1,
        "ts_ms": 1_700_000_000_000,
        "signals": {"motion_cells": 5},
        "raw_retention": "none",
        "debounce_ms": 800,
    }


def _verified(correlation_id: str = "corr-verified") -> dict:
    return {
        "type": "guard.presence.verified",
        "schema_v": 1,
        "guard_companion_id": "guard-1",
        "device_id": "atk-1",
        "correlation_id": correlation_id,
        "guard_epoch": 2,
        "ts_ms": 1_700_000_000_150,
        "verifier": "fixture",
        "verdict": "present",
        "confidence": 0.91,
        "raw_retention": "none",
    }


class _FakeRoomService:
    def __init__(self) -> None:
        self._rooms = [SimpleNamespace(name="room-body")]
        self._participants = {
            "room-body": [SimpleNamespace(identity="stackchan-1", sid="PA_body")],
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
    def __init__(self) -> None:
        self.room = _FakeRoomService()

    async def aclose(self):
        return None


async def _install_stackchan_body(store: DataStore) -> None:
    await store.companions.create(
        owner_id="owner-1",
        companion_id="companion-body",
        display_name="StackChan",
    )
    await store.devices.create_device(
        device_id="stackchan-1",
        owner_id="owner-1",
        name="StackChan",
        kind="m5stack-core-s3",
        bound_companion_id="companion-body",
        capabilities_json={"ops": [BODY_OP_PRESENCE_SET]},
    )


async def _publish_body_action(
    control: GuardControlPlane,
    *,
    correlation_id: str,
    guard_epoch: int,
) -> dict:
    payload = {
        **_candidate(),
        "correlation_id": correlation_id,
        "guard_epoch": guard_epoch,
    }
    accepted = await GuardIngress(control).handle_packet(
        topic=CONTROL_TOPIC,
        data=json.dumps(payload).encode("utf-8"),
        sender_identity="atk-1",
        source="fake_atk",
        require_guard=True,
    )
    assert accepted is not None and accepted.actions is not None
    return next(action for action in accepted.actions if action["action"] == BODY_OP_PRESENCE_SET)


async def _body_runtime(
    store: DataStore,
    *,
    online: bool = True,
) -> tuple[LiveKitAdminRuntime, _FakeLiveKitAPI]:
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg, data_store=store)
    fake_api = _FakeLiveKitAPI()
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]
    if online:
        await runtime.run_probe_cycle(["stackchan-1"])
    return runtime, fake_api


def _guard_app(
    *,
    control: GuardControlPlane,
    store: DataStore,
    runtime: LiveKitAdminRuntime,
    worker: GuardBodyActionDeliveryWorker,
) -> FastAPI:
    app = FastAPI()
    app.state.guard_control_plane = control
    app.state.guard_ingress = GuardIngress(control)
    app.state.guard_fixture_subscriber = MissionControlFixtureSubscriber(control)
    app.state.admin_runtime = runtime
    app.state.guard_body_delivery = worker
    app.state.data_store = store
    app.include_router(guard_router)
    return app


async def test_guard_fixture_loop_candidate_action_ack_and_absence(plane) -> None:
    control, store = plane
    candidate = await control.handle(_candidate(), sender_identity="atk-1")
    assert candidate.topic == CONTROL_TOPIC
    assert candidate.action is not None
    assert candidate.action["type"] == "guard.policy.action"
    assert candidate.action["action"] == "mission_control.annotate"
    # The action is an outbox record, not an in-memory fixture artifact.
    restarted = GuardControlPlane(store)
    assert [action["action_id"] for action in await restarted.pending_actions()] == [
        candidate.action["action_id"]
    ]

    ack = await control.handle(
        {
            "type": "guard.policy.action_ack",
            "schema_v": 1,
            "guard_companion_id": "guard-1",
            "device_id": "atk-1",
            "correlation_id": "corr-1",
            "guard_epoch": 1,
            "ts_ms": 1_700_000_000_100,
            "action_id": candidate.action["action_id"],
            "subscriber": "mission_control_fixture",
            "status": "completed",
        }
    )
    assert ack.action is None
    repeated_ack = await control.handle(
        {
            "type": "guard.policy.action_ack",
            "schema_v": 1,
            "guard_companion_id": "guard-1",
            "device_id": "atk-1",
            "correlation_id": "corr-1",
            "guard_epoch": 1,
            "ts_ms": 1_700_000_000_101,
            "action_id": candidate.action["action_id"],
            "subscriber": "mission_control_fixture",
            "status": "completed",
        }
    )
    assert repeated_ack.action is None
    assert await control.pending_actions() == []
    stored_action = await store.guard_actions.get(candidate.action["action_id"])
    assert stored_action is not None and stored_action.status == "acknowledged"

    verified = await control.handle(_verified(), sender_identity="atk-1")
    assert verified.action is not None
    assert verified.action["action"] == "mission_control.annotate"

    rejected = await control.handle(
        {
            "type": "guard.presence.verified",
            "schema_v": 1,
            "guard_companion_id": "guard-1",
            "device_id": "atk-1",
            "correlation_id": "corr-rejected",
            "guard_epoch": 3,
            "ts_ms": 1_700_000_000_175,
            "verifier": "fixture",
            "verdict": "rejected",
            "confidence": 0.99,
            "raw_retention": "none",
        },
        sender_identity="atk-1",
    )
    assert rejected.action is None

    absent = await control.handle(
        {
            "type": "guard.presence.absent",
            "schema_v": 1,
            "guard_companion_id": "guard-1",
            "device_id": "atk-1",
            "correlation_id": "corr-2",
            "guard_epoch": 4,
            "ts_ms": 1_700_000_000_200,
            "reason": "timeout",
            "absent_for_ms": 30_000,
            "raw_retention": "none",
        },
        sender_identity="atk-1",
    )
    assert absent.action is not None
    assert absent.action["action"] == "mission_control.clear"
    events = await store.events.list_for_owner("owner-1")
    assert {event.event_type for event in events} >= {
        "guard.presence.candidate",
        "guard.policy.evaluated",
        "guard.policy.action",
        "guard.policy.action_ack",
        "guard.presence.verified",
        "guard.presence.absent",
    }


async def test_mission_control_fixture_drains_outbox_via_admin_api(plane) -> None:
    control, store = plane
    candidate = await control.handle(_candidate(), sender_identity="atk-1")
    assert candidate.action is not None

    app = FastAPI()
    app.state.guard_control_plane = control
    app.state.guard_fixture_subscriber = MissionControlFixtureSubscriber(control)
    app.include_router(guard_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/admin/guard/fixture/drain", json={"limit": 1})

    assert response.status_code == 200
    assert response.json() == {
        "acknowledged_action_ids": [candidate.action["action_id"]],
    }
    assert await control.pending_actions() == []
    action = await store.guard_actions.get(candidate.action["action_id"])
    assert action is not None and action.status == "acknowledged"


async def test_fake_atk_ingress_uses_raw_packet_sender_identity_and_replay_dedup(plane) -> None:
    control, store = plane
    ingress = GuardIngress(control)
    payload = _candidate()

    accepted = await ingress.handle_packet(
        topic=CONTROL_TOPIC,
        data=json.dumps(payload).encode("utf-8"),
        sender_identity="atk-1",
        source="fake_atk",
        require_guard=True,
    )
    assert accepted is not None and accepted.action is not None

    replayed = await ingress.handle_packet(
        topic=CONTROL_TOPIC,
        data=json.dumps(payload).encode("utf-8"),
        sender_identity="atk-1",
        source="fake_atk",
        require_guard=True,
    )
    assert replayed is not None
    assert replayed.action["action_id"] == accepted.action["action_id"]
    assert [row.action_id for row in await store.guard_actions.list_pending()] == [
        accepted.action["action_id"]
    ]

    with pytest.raises(GuardPolicyError, match="sender identity"):
        await ingress.handle_packet(
            topic=CONTROL_TOPIC,
            data=json.dumps({**_candidate(), "correlation_id": "corr-wrong"}).encode("utf-8"),
            sender_identity="other-device",
            source="fake_atk",
            require_guard=True,
        )
    with pytest.raises(GuardPolicyError, match="fixture-only"):
        await ingress.handle_packet(
            topic=CONTROL_TOPIC,
            data=json.dumps(_verified("verified-fake-atk")).encode("utf-8"),
            sender_identity="atk-1",
            source="fake_atk",
            require_guard=True,
        )


async def test_fake_atk_admin_endpoint_is_not_the_fixture_events_path(plane) -> None:
    control, _store = plane
    app = FastAPI()
    app.state.guard_control_plane = control
    app.state.guard_ingress = GuardIngress(control)
    app.state.guard_fixture_subscriber = MissionControlFixtureSubscriber(control)
    app.include_router(guard_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/guard/fake-atk/ingress",
            content=json.dumps(_candidate()),
            headers={"X-Fake-Participant-Identity": "atk-1"},
        )
        rejected = await client.post(
            "/api/admin/guard/fake-atk/ingress",
            content=json.dumps(_verified("verified-endpoint")),
            headers={"X-Fake-Participant-Identity": "atk-1"},
        )

    assert response.status_code == 200
    assert response.json()["action"]["action"] == "mission_control.annotate"
    assert rejected.status_code == 409
    assert "fixture-only" in rejected.json()["detail"]


async def test_body_presence_delivery_uses_standard_command_result_and_ack(plane) -> None:
    control, store = plane
    await _install_stackchan_body(store)
    body_action = await _publish_body_action(
        control,
        correlation_id="corr-body",
        guard_epoch=9,
    )
    assert body_action["subscriber"] == "stackchan-1"
    assert body_action["payload"] == {"state": "awake", "presence": "candidate"}

    runtime, fake_api = await _body_runtime(store)
    worker = GuardBodyActionDeliveryWorker(store, runtime, control)
    assert await worker.reconcile_once() == 1
    row = await store.guard_actions.get(body_action["action_id"])
    assert row is not None and row.command_id
    sent = fake_api.room.sent_payloads[-1]
    assert sent.topic == CONTROL_TOPIC
    assert sent.destination_identities == ["stackchan-1"]
    envelope = json.loads(bytes(sent.data).decode("utf-8"))
    assert envelope["kind"] == "cmd"
    assert envelope["op"] == BODY_OP_PRESENCE_SET
    assert envelope["payload"] == {
        "state": "awake",
        "guard_epoch": 9,
        "correlation_id": "corr-body",
        "action_id": body_action["action_id"],
    }
    forbidden = json.dumps(envelope)
    assert "guard.policy.action" not in forbidden
    assert "face_score" not in forbidden
    assert "owner_face" not in forbidden
    assert "raw_image" not in forbidden

    app = _guard_app(control=control, store=store, runtime=runtime, worker=worker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/guard/fake-body/result",
            json={"envelope": envelope, "sender_identity": "stackchan-1"},
        )
        repeated = await client.post(
            "/api/admin/guard/fake-body/result",
            json={"envelope": envelope, "sender_identity": "stackchan-1"},
        )

    assert response.status_code == 200
    assert response.json()["command_id"] == row.command_id
    assert response.json()["action_id"] == body_action["action_id"]
    assert response.json()["guard_action_status"] == "acknowledged"
    assert response.json()["result"]["kind"] == "result"
    assert response.json()["result"]["result"] == {
        "action_id": body_action["action_id"],
        "state": "awake",
        "applied": True,
    }
    assert repeated.status_code == 200
    assert repeated.json()["guard_action_status"] == "acknowledged"
    acknowledged = await store.guard_actions.get(body_action["action_id"])
    assert acknowledged is not None and acknowledged.status == "acknowledged"
    assert acknowledged.ack_json["subscriber"] == "stackchan-1"


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "v": 1,
            "kind": "cmd",
            "id": "cmd-guard-action",
            "op": "guard.policy.action",
            "payload": {"type": "guard.policy.action"},
        },
        {
            "v": 1,
            "kind": "cmd",
            "id": "cmd-candidate",
            "op": BODY_OP_PRESENCE_SET,
            "payload": {
                "state": "awake",
                "guard_epoch": 1,
                "correlation_id": "corr-body",
                "action_id": "action-body",
                "face_score": 0.91,
            },
        },
        {
            "v": 1,
            "kind": "cmd",
            "id": "cmd-presence",
            "op": BODY_OP_PRESENCE_SET,
            "payload": {
                "state": "awake",
                "guard_epoch": 1,
                "correlation_id": "corr-body",
                "action_id": "action-body",
                "presence": "candidate",
            },
        },
        {
            "v": 1,
            "kind": "cmd",
            "id": "cmd-owner-face-score",
            "op": BODY_OP_PRESENCE_SET,
            "payload": {
                "state": "awake",
                "guard_epoch": 1,
                "correlation_id": "corr-body",
                "action_id": "action-body",
            },
            "ownerFaceScore": 0.92,
        },
    ],
)
async def test_fake_body_endpoint_rejects_guard_face_and_presence_payloads(envelope) -> None:
    app = FastAPI()
    app.include_router(guard_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/guard/fake-body/result",
            json={"envelope": envelope, "sender_identity": "stackchan-1"},
        )

    assert response.status_code == 422
    assert "fake body endpoint" in response.json()["detail"]


async def test_body_presence_delivery_records_retry_error_when_body_offline(plane) -> None:
    control, store = plane
    await _install_stackchan_body(store)
    body_action = await _publish_body_action(
        control,
        correlation_id="corr-body-offline",
        guard_epoch=11,
    )
    runtime, fake_api = await _body_runtime(store, online=False)
    worker = GuardBodyActionDeliveryWorker(store, runtime, control)

    assert await worker.reconcile_once() == 0

    row = await store.guard_actions.get(body_action["action_id"])
    assert row is not None
    assert row.status == "published"
    assert row.command_id is None
    assert row.delivery_attempt_count == 1
    assert "not currently connected" in row.last_error
    assert fake_api.room.sent_payloads == []


async def test_fake_body_result_action_mismatch_fails_guard_action(plane) -> None:
    control, store = plane
    await _install_stackchan_body(store)
    body_action = await _publish_body_action(
        control,
        correlation_id="corr-body-mismatch",
        guard_epoch=12,
    )
    runtime, fake_api = await _body_runtime(store)
    worker = GuardBodyActionDeliveryWorker(store, runtime, control)
    assert await worker.reconcile_once() == 1

    sent = fake_api.room.sent_payloads[-1]
    envelope = json.loads(bytes(sent.data).decode("utf-8"))
    app = _guard_app(control=control, store=store, runtime=runtime, worker=worker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/guard/fake-body/result",
            json={
                "envelope": envelope,
                "sender_identity": "stackchan-1",
                "action_id_override": "wrong-action",
            },
        )

    assert response.status_code == 200
    assert response.json()["action_id"] == body_action["action_id"]
    assert response.json()["guard_action_status"] == "failed"
    assert response.json()["result"]["result"]["action_id"] == "wrong-action"
    failed = await store.guard_actions.get(body_action["action_id"])
    assert failed is not None
    assert failed.status == "failed"
    assert failed.ack_json["status"] == "failed"
    assert "does not match" in failed.ack_json["message"]


async def test_guard_policy_respects_active_binding_configuration(plane) -> None:
    control, store = plane
    binding = await store.guard_bindings.get_active_for_device("atk-1")
    assert binding is not None
    await store.guard_bindings.update_config(
        binding_id=binding.binding_id,
        expected_revision=binding.config_revision,
        config_json={
            "candidate_enabled": False,
            "verified_enabled": True,
            "accepted_verdicts": ["present"],
            "absence_enabled": False,
        },
    )

    candidate = await control.handle(_candidate(), sender_identity="atk-1")
    assert candidate.action is None

    verified = await control.handle(
        {
            "type": "guard.presence.verified",
            "schema_v": 1,
            "guard_companion_id": "guard-1",
            "device_id": "atk-1",
            "correlation_id": "configured-verified",
            "guard_epoch": 2,
            "ts_ms": 1_700_000_000_200,
            "verifier": "fixture",
            "verdict": "present",
            "raw_retention": "none",
        },
        sender_identity="atk-1",
    )
    assert verified.action is not None

    absent = await control.handle(
        {
            "type": "guard.presence.absent",
            "schema_v": 1,
            "guard_companion_id": "guard-1",
            "device_id": "atk-1",
            "correlation_id": "configured-absent",
            "guard_epoch": 3,
            "ts_ms": 1_700_000_000_300,
            "reason": "timeout",
            "absent_for_ms": 10_000,
            "raw_retention": "none",
        },
        sender_identity="atk-1",
    )
    assert absent.action is None
