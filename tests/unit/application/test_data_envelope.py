from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.ingest_data_envelope import (
    DataEnvelopeRejected,
    IngestDataEnvelope,
)
from hub.domain.channels.entities import (
    ChannelDataEnvelope,
    ChannelLease,
    CommandAckData,
    CommandResultData,
    DeviceEventData,
    InboundCommandData,
    ReportedStateData,
)
from hub.domain.commands.entities import CommandState, DeviceCommand

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Channels:
    def __init__(self):
        self.lease = ChannelLease(
            channel_id="channel-1",
            device_id="device-1",
            profile_name="management-data",
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )

    async def get(self, channel_id):
        return self.lease if channel_id == self.lease.channel_id else None


class _Commands:
    def __init__(self):
        self.command = DeviceCommand(
            command_id="command-1",
            device_id="device-1",
            operation="sensor.calibrate",
            payload_json='{"offset":1}',
            state=CommandState.SENT,
            created_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=1),
            updated_at=NOW - timedelta(seconds=1),
        )

    async def get(self, command_id):
        return self.command if command_id == self.command.command_id else None

    async def upsert(self, command):
        self.command = command
        return command


class _Events:
    def __init__(self):
        self.items = []

    async def publish(self, event):
        self.items.append(event)


def _envelope(kind: str, payload: dict, *, device_id: str = "device-1"):
    if kind == "command":
        data = InboundCommandData()
    elif kind == "ack":
        data = CommandAckData(
            command_id=payload["command_id"],
            target_state={
                "accepted": CommandState.ACCEPTED,
                "running": CommandState.RUNNING,
                "rejected": CommandState.REJECTED,
            }[payload["status"]],
            error=payload.get("error", ""),
        )
    elif kind == "result":
        data = CommandResultData(
            command_id=payload["command_id"],
            target_state={
                "succeeded": CommandState.SUCCEEDED,
                "failed": CommandState.FAILED,
                "rejected": CommandState.REJECTED,
                "expired": CommandState.EXPIRED,
            }[payload["status"]],
            result_json=payload.get("result_json"),
            error=payload.get("error", ""),
        )
    elif kind == "state":
        data = ReportedStateData(revision=payload["revision"], values_json=payload["values_json"])
    else:
        data = DeviceEventData(
            event_id=payload["event_id"],
            name=payload["name"],
            payload_json=payload["payload_json"],
        )
    return ChannelDataEnvelope(
        envelope_id=f"envelope-{kind}",
        channel_id="channel-1",
        device_id=device_id,
        sequence=1,
        occurred_at=NOW,
        payload=data,
    )


def _use_case():
    channels, commands, events = _Channels(), _Commands(), _Events()
    return (
        IngestDataEnvelope(
            channels=channels,
            commands=commands,
            events=events,
            clock=_Clock(),
        ),
        channels,
        commands,
        events,
    )


async def test_ack_and_result_update_durable_command_state() -> None:
    use_case, _channels, commands, _events = _use_case()

    accepted = await use_case.execute(
        _envelope("ack", {"command_id": "command-1", "status": "accepted"})
    )
    succeeded = await use_case.execute(
        _envelope(
            "result",
            {
                "command_id": "command-1",
                "status": "succeeded",
                "result_json": '{"calibrated":true}',
            },
        )
    )

    assert accepted.state is CommandState.ACCEPTED
    assert succeeded.state is CommandState.SUCCEEDED
    assert commands.command.result_json == '{"calibrated":true}'


async def test_state_and_event_become_standard_internal_events() -> None:
    use_case, _channels, _commands, events = _use_case()

    await use_case.execute(_envelope("state", {"revision": 3, "values_json": '{"temperature":21}'}))
    await use_case.execute(
        _envelope(
            "event",
            {
                "event_id": "device-event-1",
                "name": "button.pressed",
                "payload_json": '{"button":1}',
            },
        )
    )

    assert [item.event_type for item in events.items] == [
        "eidolon.device.reported_state.v1",
        "eidolon.device.event.v1",
    ]
    assert json.loads(events.items[0].data_json)["values"]["temperature"] == 21


async def test_envelope_cannot_claim_another_device_or_send_commands() -> None:
    use_case, _channels, _commands, _events = _use_case()

    with pytest.raises(DataEnvelopeRejected, match="lease"):
        await use_case.execute(
            _envelope(
                "ack",
                {"command_id": "command-1", "status": "accepted"},
                device_id="device-2",
            )
        )
    with pytest.raises(DataEnvelopeRejected, match="cannot send command"):
        await use_case.execute(_envelope("command", {"command_id": "command-1"}))


async def test_envelope_rejects_unknown_command_and_unsupported_typed_payload() -> None:
    use_case, _channels, _commands, _events = _use_case()
    with pytest.raises(DataEnvelopeRejected, match="does not belong"):
        await use_case.execute(
            _envelope("ack", {"command_id": "unknown-command", "status": "accepted"})
        )
    unsupported = _envelope(
        "event",
        {
            "event_id": "event-1",
            "name": "button",
            "payload_json": "{}",
        },
    )
    object.__setattr__(unsupported, "payload", object())
    with pytest.raises(DataEnvelopeRejected, match="unsupported"):
        await use_case.execute(unsupported)
