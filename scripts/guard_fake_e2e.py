#!/usr/bin/env python3
"""Run the ATK Guard fake control-plane E2E without devices or LiveKit."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eidolon_data import DataSettings, DataStore  # noqa: E402
from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET, CapabilityManifest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from hub.api.routers.admin.guard import router as guard_router  # noqa: E402
from hub.config import AppConfig, LiveKitConfig  # noqa: E402
from hub.core.admin_runtime import LiveKitAdminRuntime  # noqa: E402
from hub.core.guard_body_delivery import GuardBodyActionDeliveryWorker  # noqa: E402
from hub.core.guard_fixture_subscriber import MissionControlFixtureSubscriber  # noqa: E402
from hub.core.guard_ingress import GuardIngress  # noqa: E402
from hub.core.guard_policy import GuardControlPlane  # noqa: E402
from hub.core.runtime_blackboard import OwnerRuntimeBlackboard  # noqa: E402

Scenario = Literal[
    "success",
    "offline",
    "mismatch",
    "timeout",
    "dead-letter",
    "reject-guard-body",
]


class FakeRoomService:
    def __init__(self, *, body_online: bool = True) -> None:
        self._rooms = [SimpleNamespace(name="fake-control-room")] if body_online else []
        self._participants = {
            "fake-control-room": [SimpleNamespace(identity="stackchan-1", sid="PA_fake_body")]
        }
        self.sent_payloads: list[Any] = []

    async def list_rooms(self, _request: Any) -> Any:
        return SimpleNamespace(rooms=self._rooms)

    async def list_participants(self, request: Any) -> Any:
        return SimpleNamespace(participants=self._participants.get(request.room, []))

    async def send_data(self, request: Any) -> Any:
        self.sent_payloads.append(request)
        return SimpleNamespace()


class FakeLiveKitAPI:
    def __init__(self, *, body_online: bool = True) -> None:
        self.room = FakeRoomService(body_online=body_online)

    async def aclose(self) -> None:
        return None


async def run_scenario(scenario: Scenario) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"eidolon-guard-{scenario}-") as tmp_dir:
        store = DataStore.open(DataSettings(sqlite_path=str(Path(tmp_dir) / "guard.sqlite3")))
        await store.init_schema()
        try:
            return await _run_scenario_with_store(scenario, store)
        finally:
            await store.close()


async def _run_scenario_with_store(scenario: Scenario, store: DataStore) -> dict[str, Any]:
    await _seed_guard_and_body(store)
    blackboard = await _body_blackboard()
    control = GuardControlPlane(store, runtime_blackboard=blackboard)
    runtime, fake_api = await _runtime(
        store,
        body_online=scenario not in {"offline", "dead-letter"},
    )
    worker = GuardBodyActionDeliveryWorker(
        store,
        runtime,
        control,
        max_delivery_attempts=2 if scenario == "dead-letter" else 5,
        retry_base_seconds=0 if scenario == "dead-letter" else 1,
    )
    app = _app(control=control, store=store, runtime=runtime, worker=worker)

    if scenario == "reject-guard-body":
        rejected = await _post_fake_body_result(
            app,
            {
                "v": 1,
                "kind": "cmd",
                "id": "cmd-guard-action",
                "op": "guard.policy.action",
                "payload": {"type": "guard.policy.action"},
            },
        )
        return {
            "scenario": scenario,
            "expected": "fake body rejects guard payloads before command ack",
            "http_status": rejected.status_code,
            "detail": rejected.json().get("detail"),
            "passed": rejected.status_code == 422,
        }

    accepted = await _post_fake_atk_candidate(app, correlation_id=f"corr-{scenario}")
    accepted_payload = accepted.json()
    mission_action_ids = _mission_control_action_ids(accepted_payload)
    fixture_drain = await _drain_mission_control_fixture(app)
    body_action = _find_body_action(accepted_payload)
    dispatch_count = await worker.reconcile_once()
    if scenario == "dead-letter":
        dispatch_count += await worker.reconcile_once()
    row = await store.guard_actions.get(body_action["action_id"])

    result_response: dict[str, Any] | None = None
    command_envelope: dict[str, Any] | None = None
    timeout_count = 0
    if fake_api.room.sent_payloads:
        sent = fake_api.room.sent_payloads[-1]
        command_envelope = json.loads(bytes(sent.data).decode("utf-8"))
        if scenario == "timeout":
            timeout_count = await runtime.mark_command_timeout(timeout_seconds=0)
            await worker.reconcile_command_results()
        else:
            result = await _post_fake_body_result(
                app,
                command_envelope,
                action_id_override="wrong-action" if scenario == "mismatch" else None,
            )
            result_response = result.json()

    final = await store.guard_actions.get(body_action["action_id"])
    return {
        "scenario": scenario,
        "fake_atk_status": accepted.status_code,
        "mission_control_action_ids": mission_action_ids,
        "mission_control_acknowledged_action_ids": fixture_drain["acknowledged_action_ids"],
        "body_action_id": body_action["action_id"],
        "body_subscriber": body_action["subscriber"],
        "dispatch_count": dispatch_count,
        "command_id": row.command_id if row is not None else None,
        "sent_command": _summarize_command(command_envelope),
        "fake_body_result": result_response,
        "guard_action_status": final.status if final is not None else None,
        "delivery_attempt_count": final.delivery_attempt_count if final is not None else None,
        "last_error": final.last_error if final is not None else None,
        "timeout_count": timeout_count,
        "passed": _scenario_passed(
            scenario,
            dispatch_count,
            row,
            final,
            result_response,
            mission_action_ids=mission_action_ids,
            fixture_acknowledged_action_ids=fixture_drain["acknowledged_action_ids"],
            timeout_count=timeout_count,
        ),
    }


async def _seed_guard_and_body(store: DataStore) -> None:
    await store.owners.create(owner_id="owner-1", display_name="Owner")
    await store.devices.create_device(
        device_id="atk-1",
        owner_id=None,
        capabilities_json={"guard": {"enabled": True, "protocol_versions": [1]}},
    )
    await store.guard_bindings.ensure_guard_companion(
        owner_id="owner-1",
        companion_id="guard-1",
    )
    await store.guard_bindings.claim(
        owner_id="owner-1",
        device_id="atk-1",
        guard_companion_id="guard-1",
    )
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


async def _runtime(
    store: DataStore,
    *,
    body_online: bool,
) -> tuple[LiveKitAdminRuntime, FakeLiveKitAPI]:
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg, data_store=store)
    fake_api = FakeLiveKitAPI(body_online=body_online)
    runtime._build_livekit_api = lambda: fake_api  # type: ignore[method-assign]
    await runtime.run_probe_cycle(["stackchan-1"])
    return runtime, fake_api


async def _body_blackboard() -> OwnerRuntimeBlackboard:
    blackboard = OwnerRuntimeBlackboard()
    await blackboard.register_device_manifest(
        device_id="stackchan-1",
        manifest=CapabilityManifest.model_validate(
            {
                "capabilities": [
                    {
                        "name": BODY_OP_PRESENCE_SET,
                        "version": 1,
                        "description": "Update local presence state",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                        "result_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    }
                ]
            }
        ),
        owner_id="owner-1",
        provider_companion_id="companion-body",
        name="StackChan",
    )
    await blackboard.mark_device_online(
        owner_id="owner-1",
        device_id="stackchan-1",
        room_name="fake-control-room",
        participant_sid="PA_fake_body",
        presence_revision="PA_fake_body",
    )
    return blackboard


def _app(
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


async def _post_fake_atk_candidate(app: FastAPI, *, correlation_id: str) -> Any:
    candidate = {
        "type": "guard.presence.candidate",
        "schema_v": 1,
        "guard_companion_id": "guard-1",
        "device_id": "atk-1",
        "correlation_id": correlation_id,
        "guard_epoch": 42,
        "ts_ms": 1_700_000_000_000,
        "signals": {"observation_sequence": 1, "runtime_revision": 1},
        "camera": {"motion_score": 0.2},
        "raw_retention": "none",
        "debounce_ms": 800,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fake") as client:
        response = await client.post(
            "/api/admin/guard/fake-atk/ingress",
            content=json.dumps(candidate),
            headers={"X-Fake-Participant-Identity": "atk-1"},
        )
    response.raise_for_status()
    return response


async def _post_fake_body_result(
    app: FastAPI,
    envelope: dict[str, Any],
    *,
    action_id_override: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "envelope": envelope,
        "sender_identity": "stackchan-1",
    }
    if action_id_override is not None:
        payload["action_id_override"] = action_id_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fake") as client:
        return await client.post("/api/admin/guard/fake-body/result", json=payload)


async def _drain_mission_control_fixture(app: FastAPI) -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fake") as client:
        response = await client.post("/api/admin/guard/fixture/drain", json={"limit": 50})
    response.raise_for_status()
    return response.json()


def _find_body_action(response_json: dict[str, Any]) -> dict[str, Any]:
    for action in response_json.get("actions") or []:
        if action.get("action") == BODY_OP_PRESENCE_SET:
            return action
    raise RuntimeError("fake ATK candidate did not produce a body.presence.set action")


def _mission_control_action_ids(response_json: dict[str, Any]) -> list[str]:
    return [
        str(action["action_id"])
        for action in response_json.get("actions") or []
        if action.get("subscriber") == "mission_control_fixture"
    ]


def _summarize_command(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if envelope is None:
        return None
    return {
        "v": envelope.get("v"),
        "kind": envelope.get("kind"),
        "id": envelope.get("id"),
        "op": envelope.get("op"),
        "dst": envelope.get("dst"),
        "payload": envelope.get("payload"),
    }


def _scenario_passed(
    scenario: Scenario,
    dispatch_count: int,
    row: Any,
    final: Any,
    result_response: dict[str, Any] | None,
    *,
    mission_action_ids: list[str],
    fixture_acknowledged_action_ids: list[str],
    timeout_count: int,
) -> bool:
    if final is None:
        return False
    if not mission_action_ids or set(mission_action_ids) != set(fixture_acknowledged_action_ids):
        return False
    if scenario == "success":
        return dispatch_count == 1 and bool(row and row.command_id) and final.status == "acknowledged"
    if scenario == "offline":
        return (
            dispatch_count == 0
            and row is not None
            and row.command_id is None
            and final.status == "published"
            and final.delivery_attempt_count == 1
            and "not currently connected" in (final.last_error or "")
        )
    if scenario == "mismatch":
        return (
            dispatch_count == 1
            and result_response is not None
            and result_response.get("guard_action_status") == "failed"
            and final.status == "failed"
        )
    if scenario == "timeout":
        return (
            dispatch_count == 1
            and timeout_count == 1
            and row is not None
            and final.status == "failed"
            and "no ack/result" in (final.ack_json or {}).get("message", "")
        )
    if scenario == "dead-letter":
        return (
            dispatch_count == 0
            and row is not None
            and final.status == "failed"
            and final.delivery_attempt_count == 2
            and "exhausted after 2 attempts" in (final.ack_json or {}).get("message", "")
        )
    return False


async def _main_async(args: argparse.Namespace) -> int:
    scenarios: list[Scenario]
    if args.scenario == "all":
        scenarios = [
            "success",
            "offline",
            "mismatch",
            "timeout",
            "dead-letter",
            "reject-guard-body",
        ]
    else:
        scenarios = [args.scenario]
    results = [await run_scenario(scenario) for scenario in scenarios]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item.get("passed") for item in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=[
            "all",
            "success",
            "offline",
            "mismatch",
            "timeout",
            "dead-letter",
            "reject-guard-body",
        ],
        default="all",
        help="Fake E2E scenario to run.",
    )
    return asyncio.run(_main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
