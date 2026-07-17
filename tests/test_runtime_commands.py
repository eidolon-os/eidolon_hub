from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.api.routers.runtime.commands import router


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

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return {
            "command_id": "cmd-1",
            "device_id": kwargs["device_id"],
            "runtime_caller_id": kwargs.get("runtime_caller_id"),
            "runtime_session_id": kwargs.get("runtime_session_id"),
            "source_device_id": kwargs.get("source_device_id"),
            "topic": kwargs["topic"],
            "op": kwargs["op"],
            "status": "sent",
            "created_at": "2026-07-17T00:00:00+00:00",
            "payload": kwargs["payload"],
            "envelope": {},
            "ttl_ms": kwargs["ttl_ms"],
            "qos": kwargs["qos"],
            "priority": kwargs["priority"],
            "updated_at": "2026-07-17T00:00:00+00:00",
        }


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
                "ops": capabilities
                if capabilities is not None
                else [{"name": "device.roll_call"}]
            },
            access_policy_json={"capability_visibility": visibility},
        ),
    }
    app.state.data_store = SimpleNamespace(
        companions=_Repo(companions),
        devices=_Repo(devices),
    )
    app.state.admin_runtime = _Runtime()
    return TestClient(app), app.state.admin_runtime


def _body():
    return {
        "requester_owner_id": "owner-1",
        "requester_companion_id": "companion-a",
        "source_device_id": "box-3",
        "op": "device.roll_call",
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


def test_undeclared_or_unknown_capability_is_not_dispatched():
    client, runtime = _client(capabilities=[{"name": "vendor.unreviewed"}])

    response = client.post("/api/runtime/devices/atk-guard/commands", json=_body())

    assert response.status_code == 403
    assert runtime.sent == []
