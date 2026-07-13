from __future__ import annotations

import pytest
from eidolon_data import DataSettings, DataStore

from hub.core.guard_runtime_reconciler import GuardRuntimeReconciler


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.online = True

    async def send_command(self, device_id: str, payload: dict, **kwargs):
        if not self.online:
            raise ValueError(f"Device {device_id} is not currently connected")
        self.calls.append({"device_id": device_id, "payload": payload, **kwargs})
        return {"command_id": f"cmd-{len(self.calls)}"}


@pytest.fixture
async def store(tmp_path):
    value = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "guard-runtime.sqlite3")))
    await value.init_schema()
    try:
        yield value
    finally:
        await value.close()


async def _binding(store: DataStore):
    await store.owners.create(owner_id="owner-1", display_name="Owner")
    await store.devices.create_device(
        device_id="atk-1",
        owner_id=None,
        capabilities_json={"guard": {"enabled": True}},
    )
    return await store.guard_bindings.claim(
        owner_id="owner-1",
        device_id="atk-1",
        guard_companion_id="guard-1",
    )


@pytest.mark.asyncio
async def test_reconciler_keeps_offline_delivery_pending_and_dispatches_when_online(store: DataStore):
    binding = await _binding(store)
    runtime = _Runtime()
    runtime.online = False
    reconciler = GuardRuntimeReconciler(store, runtime)  # type: ignore[arg-type]

    assert await reconciler.reconcile_once() == 0
    row = (await store.guard_runtime_deliveries.list_for_binding(binding.binding_id))[0]
    assert row.status == "pending"
    assert row.attempt_count == 1

    runtime.online = True
    assert await reconciler.reconcile_once() == 1
    row = (await store.guard_runtime_deliveries.list_for_binding(binding.binding_id))[0]
    assert row.status == "dispatched"
    assert row.command_id == "cmd-1"
    assert runtime.calls[0]["payload"] == {
        "binding_id": binding.binding_id,
        "runtime_revision": 1,
        "desired_runtime_state": "running",
    }
    assert runtime.calls[0]["op"] == "guard.runtime.sync"
    assert runtime.calls[0]["qos"] == "result"


@pytest.mark.asyncio
async def test_reconciler_records_terminal_result_and_audit(store: DataStore):
    binding = await _binding(store)
    runtime = _Runtime()
    reconciler = GuardRuntimeReconciler(store, runtime)  # type: ignore[arg-type]
    await reconciler.reconcile_once()

    await reconciler.apply_command_result(
        {
            "command_id": "cmd-1",
            "op": "guard.runtime.sync",
            "status": "succeeded",
            "result": {
                "binding_id": binding.binding_id,
                "runtime_revision": 1,
                "desired_runtime_state": "running",
                "running": True,
            },
        }
    )

    row = (await store.guard_runtime_deliveries.list_for_binding(binding.binding_id))[0]
    assert row.status == "applied"
    assert row.result_json and row.result_json["running"] is True
    events = await store.events.list_for_owner("owner-1")
    applied = [event for event in events if event.event_type == "guard.runtime.applied"]
    assert applied[-1].payload_json["command_id"] == "cmd-1"


@pytest.mark.asyncio
async def test_reconciler_rejects_a_success_result_for_a_different_runtime_delivery(store: DataStore):
    binding = await _binding(store)
    runtime = _Runtime()
    reconciler = GuardRuntimeReconciler(store, runtime)  # type: ignore[arg-type]
    await reconciler.reconcile_once()

    await reconciler.apply_command_result(
        {
            "command_id": "cmd-1",
            "op": "guard.runtime.sync",
            "status": "succeeded",
            "result": {
                "binding_id": "gb-other",
                "runtime_revision": 1,
                "desired_runtime_state": "running",
            },
        }
    )

    row = (await store.guard_runtime_deliveries.list_for_binding(binding.binding_id))[0]
    assert row.status == "failed"
    assert row.last_error == "guard runtime result does not match delivery"
