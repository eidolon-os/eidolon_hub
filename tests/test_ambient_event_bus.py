from __future__ import annotations

import json
from time import time
from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.contracts import EVENT_TOPIC
from eidolon_sdk.biz.events import (
    AMBIENT_PRESENCE_CHANGED_TYPE,
    EVENT_MAX_BYTES,
)

from hub.config import AmbientEventBusConfig, AppConfig, _ambient_event_bus_from_yaml
from hub.core.ambient_event_bus import AmbientEventBus, AmbientEventBusError


class _Devices:
    def __init__(self) -> None:
        self.rows = {
            "box3-1": SimpleNamespace(device_id="box3-1", owner_id="owner-1", status="active"),
            "atk-1": SimpleNamespace(device_id="atk-1", owner_id="owner-1", status="active"),
            "atk-2": SimpleNamespace(device_id="atk-2", owner_id="owner-2", status="active"),
            "unowned": SimpleNamespace(device_id="unowned", owner_id=None, status="active"),
            "disabled": SimpleNamespace(
                device_id="disabled",
                owner_id="owner-1",
                status="disabled",
            ),
        }

    async def get_device(self, device_id: str):
        return self.rows.get(device_id)

    async def list_devices_for_owner(self, owner_id: str):
        return [row for row in self.rows.values() if row.owner_id == owner_id]


class _Runtime:
    async def get_presence_snapshot(self):
        return [
            SimpleNamespace(
                device_id="box3-1",
                status="online",
                room_name="control-box3",
            ),
            SimpleNamespace(
                device_id="atk-1",
                status="online",
                room_name="control-atk-1",
            ),
            SimpleNamespace(
                device_id="atk-2",
                status="online",
                room_name="control-atk-2",
            ),
            SimpleNamespace(
                device_id="disabled",
                status="online",
                room_name="control-disabled",
            ),
        ]


class _RoomService:
    def __init__(self) -> None:
        self.sent = []

    async def send_data(self, request):
        self.sent.append(request)
        return SimpleNamespace()


class _LiveKit:
    def __init__(self) -> None:
        self.room = _RoomService()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _event(**patch: object) -> dict[str, object]:
    now_ms = int(time() * 1_000)
    event: dict[str, object] = {
        "schema_v": 1,
        "kind": "event",
        "event_id": "evt-radar-1",
        "flow_id": "flow-radar-1",
        "causation_id": "",
        "type": AMBIENT_PRESENCE_CHANGED_TYPE,
        "source": {"device_id": "spoofed-device", "component": "radar"},
        "occurred_at_ms": now_ms,
        "expires_at_ms": now_ms + 3_000,
        "payload": {
            "state": "present",
            "modality": "mmwave",
            "edge": "vacant_to_present",
        },
    }
    event.update(patch)
    return event


def _bus(
    *,
    enabled: bool = True,
    owner_rate_per_second: int = 5,
    owner_rate_burst: int = 10,
    monotonic_clock=None,
):
    config = AppConfig()
    config.ambient_event_bus = AmbientEventBusConfig(
        enabled=enabled,
        owner_rate_per_second=owner_rate_per_second,
        owner_rate_burst=owner_rate_burst,
    )
    livekit = _LiveKit()
    kwargs = {}
    if monotonic_clock is not None:
        kwargs["monotonic_clock"] = monotonic_clock
    bus = AmbientEventBus(
        config,
        data_store=SimpleNamespace(devices=_Devices()),
        runtime=_Runtime(),
        livekit_api_factory=lambda: livekit,
        **kwargs,
    )
    return bus, livekit


def test_event_bus_config_defaults_and_bounds_rate_limits():
    defaults = _ambient_event_bus_from_yaml({})
    assert defaults.owner_rate_per_second == 5
    assert defaults.owner_rate_burst == 10

    bounded = _ambient_event_bus_from_yaml(
        {
            "ambient_event_bus": {
                "owner_rate_per_second": 1_000,
                "owner_rate_burst": 10_000,
            }
        }
    )
    assert bounded.owner_rate_per_second == 100
    assert bounded.owner_rate_burst == 1_000


@pytest.mark.asyncio
async def test_broadcasts_to_every_online_device_in_owner_scope_and_rewrites_source():
    bus, livekit = _bus()

    result = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=json.dumps(_event()).encode(),
        sender_identity="box3-1",
    )

    assert result is not None
    assert result.owner_id == "owner-1"
    assert result.recipient_device_ids == ("atk-1", "box3-1")
    assert {request.room for request in livekit.room.sent} == {
        "control-atk-1",
        "control-box3",
    }
    assert all(request.topic == EVENT_TOPIC for request in livekit.room.sent)
    assert all(request.destination_identities != ["atk-2"] for request in livekit.room.sent)
    payloads = [json.loads(bytes(request.data)) for request in livekit.room.sent]
    assert all(
        payload["source"] == {"device_id": "box3-1", "component": "radar"} for payload in payloads
    )
    assert livekit.closed is True


@pytest.mark.asyncio
async def test_duplicate_event_is_not_broadcast_twice():
    bus, livekit = _bus()
    encoded = json.dumps(_event()).encode()

    first = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=encoded,
        sender_identity="box3-1",
    )
    second = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=encoded,
        sender_identity="box3-1",
    )

    assert first is not None and first.recipient_device_ids
    assert second is not None and second.recipient_device_ids == ()
    assert len(livekit.room.sent) == 2


@pytest.mark.asyncio
async def test_owner_rate_limit_is_bounded_refillable_and_owner_scoped():
    now = [100.0]
    bus, livekit = _bus(
        owner_rate_per_second=2,
        owner_rate_burst=2,
        monotonic_clock=lambda: now[0],
    )

    for sequence in (1, 2):
        result = await bus.handle_packet(
            topic=EVENT_TOPIC,
            data=json.dumps(
                _event(
                    event_id=f"evt-owner1-{sequence}",
                    flow_id=f"flow-owner1-{sequence}",
                )
            ).encode(),
            sender_identity="box3-1",
        )
        assert result is not None and result.recipient_device_ids

    with pytest.raises(AmbientEventBusError, match="rate limit"):
        await bus.handle_packet(
            topic=EVENT_TOPIC,
            data=json.dumps(
                _event(event_id="evt-owner1-3", flow_id="flow-owner1-3")
            ).encode(),
            sender_identity="box3-1",
        )

    other_owner = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=json.dumps(
            _event(event_id="evt-owner2-1", flow_id="flow-owner2-1")
        ).encode(),
        sender_identity="atk-2",
    )
    assert other_owner is not None
    assert other_owner.recipient_device_ids == ("atk-2",)

    now[0] += 0.5
    refilled = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=json.dumps(
            _event(event_id="evt-owner1-4", flow_id="flow-owner1-4")
        ).encode(),
        sender_identity="box3-1",
    )
    assert refilled is not None and refilled.recipient_device_ids
    assert len(livekit.room.sent) == 7


@pytest.mark.asyncio
async def test_rejects_unowned_publisher():
    bus, livekit = _bus()

    with pytest.raises(AmbientEventBusError, match="active owner binding"):
        await bus.handle_packet(
            topic=EVENT_TOPIC,
            data=json.dumps(_event()).encode(),
            sender_identity="unowned",
        )

    assert livekit.room.sent == []


@pytest.mark.asyncio
async def test_rejects_non_active_publisher_and_excludes_non_active_recipient():
    bus, livekit = _bus()

    with pytest.raises(AmbientEventBusError, match="active owner binding"):
        await bus.handle_packet(
            topic=EVENT_TOPIC,
            data=json.dumps(_event()).encode(),
            sender_identity="disabled",
        )

    result = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=json.dumps(_event()).encode(),
        sender_identity="box3-1",
    )

    assert result is not None
    assert "disabled" not in result.recipient_device_ids
    assert all(
        "disabled" not in request.destination_identities for request in livekit.room.sent
    )


@pytest.mark.asyncio
async def test_rejects_expired_or_oversized_event():
    bus, livekit = _bus()
    now_ms = int(time() * 1_000)

    with pytest.raises(ValueError, match="expired"):
        await bus.handle_packet(
            topic=EVENT_TOPIC,
            data=json.dumps(
                _event(occurred_at_ms=now_ms - 3_000, expires_at_ms=now_ms - 1)
            ).encode(),
            sender_identity="box3-1",
        )

    with pytest.raises(AmbientEventBusError, match="transport size"):
        await bus.handle_packet(
            topic=EVENT_TOPIC,
            data=b"x" * (EVENT_MAX_BYTES + 1),
            sender_identity="box3-1",
        )

    assert livekit.room.sent == []


@pytest.mark.asyncio
async def test_disabled_bus_ignores_event_without_parsing_or_sending():
    bus, livekit = _bus(enabled=False)

    result = await bus.handle_packet(
        topic=EVENT_TOPIC,
        data=b"not-json",
        sender_identity="box3-1",
    )

    assert result is None
    assert livekit.room.sent == []
