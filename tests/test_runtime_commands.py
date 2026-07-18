from __future__ import annotations

import asyncio
from types import SimpleNamespace

from eidolon_sdk.biz.body import CapabilityManifest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.api.routers.runtime.commands import router
from hub.core.runtime_blackboard import OwnerRuntimeBlackboard


class _Repo:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        return self._rows.get(key)

    async def get_device(self, key):
        return self._rows.get(key)


class _Runtime:
    def __init__(self):
        self.sent = []
        self.commands = {}

    async def get_command(self, command_id):
        return self.commands.get(command_id)

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        command = {
            "command_id": kwargs["command_id"],
            "device_id": kwargs["device_id"],
            "runtime_caller_id": kwargs.get("runtime_caller_id"),
            "runtime_session_id": kwargs.get("runtime_session_id"),
            "runtime_trace_id": kwargs.get("runtime_trace_id"),
            "runtime_turn_id": kwargs.get("runtime_turn_id"),
            "runtime_tool_call_id": kwargs.get("runtime_tool_call_id"),
            "idempotency_key": kwargs.get("idempotency_key"),
            "owner_id": kwargs.get("requester_owner_id"),
            "requester_companion_id": kwargs.get("requester_companion_id"),
            "source_device_id": kwargs.get("source_device_id"),
            "topic": kwargs["topic"],
            "op": kwargs["op"],
            "capability_version": kwargs["capability_version"],
            "status": "sent",
            "created_at": "2026-07-17T00:00:00+00:00",
            "payload": kwargs["payload"],
            "envelope": {},
            "ttl_ms": kwargs["ttl_ms"],
            "qos": kwargs["qos"],
            "priority": kwargs["priority"],
            "updated_at": "2026-07-17T00:00:00+00:00",
        }
        self.commands[command["command_id"]] = command
        return command


def _client(*, visibility="owner", capabilities=None, target_owner="owner-1"):
    app = FastAPI()
    app.include_router(router)
    companions = {
        "companion-a": SimpleNamespace(
            companion_id="companion-a", owner_id="owner-1", status="active"
        ),
        "companion-b": SimpleNamespace(
            companion_id="companion-b", owner_id="owner-1", status="active"
        ),
    }
    devices = {
        "box-3": SimpleNamespace(
            device_id="box-3",
            owner_id="owner-1",
            bound_companion_id="companion-a",
            status="active",
            revoked_at=None,
            kind="esp-box-3",
            capabilities_json={"ops": []},
            access_policy_json={},
        ),
        "atk-guard": SimpleNamespace(
            device_id="atk-guard",
            owner_id=target_owner,
            bound_companion_id="companion-b",
            status="active",
            revoked_at=None,
            kind="atk-guard",
            capabilities_json={
                "ops": capabilities if capabilities is not None else [{"name": "device.roll_call"}]
            },
            access_policy_json={"capability_visibility": visibility},
        ),
    }
    app.state.data_store = SimpleNamespace(
        companions=_Repo(companions),
        devices=_Repo(devices),
    )
    blackboard = OwnerRuntimeBlackboard()
    declared = capabilities if capabilities is not None else [_capability("device.roll_call")]
    asyncio.run(
        blackboard.register_device_manifest(
            device_id="atk-guard",
            manifest=CapabilityManifest.model_validate({"capabilities": declared}),
            owner_id=target_owner,
            provider_companion_id="companion-b",
            name="ATK Guard",
            visibility=visibility,
            registration_id="reg-guard",
        )
    )
    asyncio.run(
        blackboard.mark_device_online(
            owner_id=target_owner,
            device_id="atk-guard",
            room_name="guard-control",
            participant_sid="PA_GUARD",
            presence_revision="presence-guard",
        )
    )
    app.state.runtime_blackboard = blackboard
    app.state.admin_runtime = _Runtime()
    return TestClient(app), app.state.admin_runtime


def _capability(name: str) -> dict:
    return {
        "name": name,
        "version": 1,
        "description": f"Execute {name} on this device.",
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


def _body():
    return {
        "requester_owner_id": "owner-1",
        "requester_companion_id": "companion-a",
        "source_device_id": "box-3",
        "op": "device.roll_call",
        "capability_version": 1,
        "idempotency_key": "test-runtime-command",
        "runtime_trace_id": "trace-1",
        "runtime_turn_id": "turn-1",
        "runtime_tool_call_id": "call-1",
        "payload": {},
        "qos": "result",
        "ttl_ms": 5000,
    }


def test_cross_companion_roll_call_is_authorized_for_same_owner():
    client, runtime = _client()

    response = client.post("/api/runtime/devices/atk-guard/commands", json=_body())

    assert response.status_code == 200
    assert response.json()["op"] == "device.roll_call"
    assert runtime.sent[0]["requester_companion_id"] == "companion-a"
    assert runtime.sent[0]["device_id"] == "atk-guard"


def test_bound_companion_visibility_blocks_cross_companion_call():
    client, runtime = _client(visibility="bound_companion")

    response = client.post("/api/runtime/devices/atk-guard/commands", json=_body())

    assert response.status_code == 403
    assert runtime.sent == []


def test_cross_owner_target_is_rejected_at_hub_boundary():
    client, runtime = _client(target_owner="owner-2")

    response = client.post("/api/runtime/devices/atk-guard/commands", json=_body())

    assert response.status_code == 403
    assert runtime.sent == []


def test_undeclared_capability_is_not_dispatched():
    client, runtime = _client(capabilities=[_capability("vendor.unreviewed")])

    response = client.post("/api/runtime/devices/atk-guard/commands", json=_body())

    assert response.status_code == 409
    assert runtime.sent == []


def test_dynamically_declared_unknown_capability_is_dispatched():
    client, runtime = _client(capabilities=[_capability("vendor.unreviewed")])
    body = _body()
    body["op"] = "vendor.unreviewed"

    response = client.post("/api/runtime/devices/atk-guard/commands", json=body)

    assert response.status_code == 200
    assert runtime.sent[0]["op"] == "vendor.unreviewed"


def test_contract_version_mismatch_is_rejected_before_dispatch():
    client, runtime = _client()
    body = _body()
    body["capability_version"] = 2

    response = client.post("/api/runtime/devices/atk-guard/commands", json=body)

    assert response.status_code == 409
    assert "device.roll_call" in response.json()["detail"]
    assert runtime.sent == []


def test_same_idempotency_key_returns_existing_command_without_redispatch():
    client, runtime = _client()
    body = _body()

    first = client.post("/api/runtime/devices/atk-guard/commands", json=body)
    second = client.post("/api/runtime/devices/atk-guard/commands", json=body)

    assert first.status_code == second.status_code == 200
    assert first.json()["command_id"] == second.json()["command_id"]
    assert len(runtime.sent) == 1


def test_reusing_idempotency_key_for_different_payload_is_rejected():
    client, runtime = _client()
    first = client.post("/api/runtime/devices/atk-guard/commands", json=_body())
    changed = _body()
    changed["payload"] = {"unexpected": True}
    second = client.post("/api/runtime/devices/atk-guard/commands", json=changed)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "idempotency key" in second.json()["detail"]
    assert len(runtime.sent) == 1


def test_runtime_blackboard_http_projection_is_removed():
    client, _runtime = _client()
    assert client.get("/api/runtime/blackboard").status_code == 404
