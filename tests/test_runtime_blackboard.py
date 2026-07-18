from __future__ import annotations

from datetime import UTC, datetime

import pytest
from eidolon_sdk.biz.body import (
    CapabilityManifest,
    OwnerDeviceBlackboardSnapshot,
    owner_device_blackboard_key,
)

from hub.core.runtime_blackboard import (
    OwnerRuntimeBlackboard,
    RuntimeCapabilityUnavailable,
)


class _FakeKV:
    def __init__(self):
        self.values: dict[str, bytes] = {"stale.current": b"stale"}

    async def get(self, key: str):
        return self.values.get(key)

    async def clear(self):
        self.values.clear()

    async def put(self, key: str, value: bytes):
        self.values[key] = value

    async def delete(self, key: str):
        self.values.pop(key, None)

    async def keys(self, prefix: str = ""):
        return sorted(key for key in self.values if key.startswith(prefix))


def _manifest(*names: str) -> CapabilityManifest:
    return CapabilityManifest.model_validate(
        {
            "capabilities": [
                {
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
                for name in names
            ]
        }
    )


async def _register(board: OwnerRuntimeBlackboard, *, visibility: str = "owner"):
    return await board.register_device_manifest(
        device_id="guard-1",
        manifest=_manifest("device.roll_call"),
        owner_id="owner-1",
        provider_companion_id="companion-guard",
        provider_companion_name="Guard",
        name="ATK Guard",
        aliases=("Guard", "Guard"),
        visibility=visibility,
        registration_id="reg-1",
        registered_at=datetime(2026, 7, 17, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_initialization_replaces_stale_state_with_one_snapshot_per_owner() -> None:
    kv = _FakeKV()
    board = OwnerRuntimeBlackboard(kv, epoch="epoch-new")

    await board.initialize(["owner-1", "owner-2"])

    assert "stale.current" not in kv.values
    assert set(kv.values) == {
        owner_device_blackboard_key("owner-1"),
        owner_device_blackboard_key("owner-2"),
    }
    first = OwnerDeviceBlackboardSnapshot.from_bytes(
        kv.values[owner_device_blackboard_key("owner-1")],
        expected_owner_id="owner-1",
    )
    second = OwnerDeviceBlackboardSnapshot.from_bytes(
        kv.values[owner_device_blackboard_key("owner-2")],
        expected_owner_id="owner-2",
    )
    assert first.epoch == second.epoch == "epoch-new"
    assert first.ready is second.ready is False
    assert first.devices == second.devices == {}

    await board.mark_ready(["owner-1", "owner-2"])
    await _register(board)
    first = await board.read_owner_snapshot("owner-1")
    second = await board.read_owner_snapshot("owner-2")
    assert first is not None and set(first.devices) == {"guard-1"}
    assert second is not None and second.devices == {}


@pytest.mark.asyncio
async def test_registered_manifest_is_not_visible_until_transport_is_online() -> None:
    board = OwnerRuntimeBlackboard()
    entry = await _register(board)

    assert entry.status == "registered_waiting_transport"
    assert (
        await board.list_visible_runtime_devices(
            owner_id="owner-1", requester_companion_id="companion-a"
        )
        == []
    )

    online = await board.mark_device_online(
        owner_id="owner-1",
        device_id="guard-1",
        room_name="guard-control",
        participant_sid="PA_OLD_TOKEN",
        presence_revision="presence-old-token",
    )

    assert online is not None
    visible = await board.list_visible_runtime_devices(
        owner_id="owner-1", requester_companion_id="companion-a"
    )
    assert [item.device_id for item in visible] == ["guard-1"]
    assert visible[0].aliases == ("Guard",)
    assert visible[0].provider_companion_name == "Guard"


@pytest.mark.asyncio
async def test_registration_atomically_replaces_manifest_and_empty_list_clears_it() -> None:
    board = OwnerRuntimeBlackboard()
    first = await _register(board)
    await board.mark_device_online(
        owner_id="owner-1",
        device_id="guard-1",
        room_name="guard-control",
        participant_sid="PA_EXISTING",
        presence_revision="presence-existing",
    )
    replacement = await board.register_device_manifest(
        device_id="guard-1",
        manifest=_manifest("camera.capture"),
        owner_id="owner-1",
        provider_companion_id="companion-guard",
        name="ATK Guard",
        registration_id="reg-2",
    )

    assert replacement.registration_id != first.registration_id
    assert [item.name for item in replacement.capabilities] == ["camera.capture"]
    assert replacement.status == "online"
    assert replacement.participant_sid == "PA_EXISTING"

    cleared = await board.register_device_manifest(
        device_id="guard-1",
        manifest=_manifest(),
        owner_id="owner-1",
        provider_companion_id="companion-guard",
        name="ATK Guard",
        registration_id="reg-3",
    )
    assert cleared.capabilities == ()


@pytest.mark.asyncio
async def test_visibility_and_owner_boundaries_are_enforced_by_the_blackboard() -> None:
    board = OwnerRuntimeBlackboard()
    await _register(board, visibility="bound_companion")
    await board.mark_device_online(
        owner_id="owner-1",
        device_id="guard-1",
        room_name="guard-control",
        participant_sid="PA_GUARD",
        presence_revision="presence-1",
    )

    assert (
        await board.list_visible_runtime_devices(
            owner_id="owner-1", requester_companion_id="companion-a"
        )
        == []
    )
    assert (
        len(
            await board.list_visible_runtime_devices(
                owner_id="owner-1", requester_companion_id="companion-guard"
            )
        )
        == 1
    )
    assert (
        await board.list_visible_runtime_devices(
            owner_id="owner-2", requester_companion_id="companion-guard"
        )
        == []
    )


@pytest.mark.asyncio
async def test_current_capability_resolution_fails_closed_after_disconnect() -> None:
    board = OwnerRuntimeBlackboard()
    await _register(board)
    await board.mark_device_online(
        owner_id="owner-1",
        device_id="guard-1",
        room_name="guard-control",
        participant_sid="PA_GUARD",
        presence_revision="presence-1",
    )

    entry, capability = await board.resolve_current_capability(
        owner_id="owner-1",
        requester_companion_id="companion-a",
        device_id="guard-1",
        capability_name="device.roll_call",
        capability_version=1,
    )
    assert entry.device_id == "guard-1"
    assert capability.name == "device.roll_call"

    assert await board.remove_device_session(owner_id="owner-1", device_id="guard-1") is True
    with pytest.raises(RuntimeCapabilityUnavailable, match="not online"):
        await board.resolve_current_capability(
            owner_id="owner-1",
            requester_companion_id="companion-a",
            device_id="guard-1",
            capability_name="device.roll_call",
            capability_version=1,
        )
