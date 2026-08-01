"""Validate provider-neutral data-channel messages and update Hub state."""

from __future__ import annotations

import json

from hub.domain.channels.entities import (
    ChannelDataEnvelope,
    CommandAckData,
    CommandResultData,
    DeviceEventData,
    InboundCommandData,
    ReportedStateData,
)
from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.commands.state_machine import transition_command
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import ChannelLeaseRepository, CommandRepository


class DataEnvelopeRejected(ValueError):
    pass


class IngestDataEnvelope:
    def __init__(
        self,
        *,
        channels: ChannelLeaseRepository,
        commands: CommandRepository,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._channels = channels
        self._commands = commands
        self._events = events
        self._clock = clock

    async def execute(self, envelope: ChannelDataEnvelope) -> DeviceCommand | None:
        now = self._clock.now()
        lease = await self._channels.get(envelope.channel_id)
        if lease is None or lease.device_id != envelope.device_id or lease.expires_at <= now:
            raise DataEnvelopeRejected("active channel lease required")
        if isinstance(envelope.payload, InboundCommandData):
            raise DataEnvelopeRejected("devices cannot send command envelopes")
        if isinstance(envelope.payload, CommandAckData):
            payload = envelope.payload
            return await self._update_command(
                envelope=envelope,
                command_id=payload.command_id,
                target=payload.target_state,
                error=payload.error,
            )
        if isinstance(envelope.payload, CommandResultData):
            payload = envelope.payload
            return await self._update_command(
                envelope=envelope,
                command_id=payload.command_id,
                target=payload.target_state,
                error=payload.error,
                result_json=payload.result_json,
            )
        if isinstance(envelope.payload, ReportedStateData):
            payload = envelope.payload
            data_json = json.dumps(
                {
                    "revision": payload.revision,
                    "values": json.loads(payload.values_json),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            event_type = "eidolon.device.reported_state.v1"
            event_id = envelope.envelope_id
        else:
            if not isinstance(envelope.payload, DeviceEventData):
                raise DataEnvelopeRejected("unsupported channel data payload")
            payload = envelope.payload
            data_json = json.dumps(
                {"name": payload.name, "payload": json.loads(payload.payload_json)},
                sort_keys=True,
                separators=(",", ":"),
            )
            event_type = "eidolon.device.event.v1"
            event_id = payload.event_id
        await self._events.publish(
            DomainEvent(
                event_id=event_id,
                event_type=event_type,
                source="eidolon-hub/channel-data",
                subject=envelope.device_id,
                occurred_at=now,
                data_json=data_json,
            )
        )
        return None

    async def _update_command(
        self,
        *,
        envelope: ChannelDataEnvelope,
        command_id: str,
        target: CommandState,
        error: str = "",
        result_json: str | None = None,
    ) -> DeviceCommand:
        command = await self._commands.get(command_id)
        if command is None or command.device_id != envelope.device_id:
            raise DataEnvelopeRejected("envelope command does not belong to device")
        updated = transition_command(
            command,
            target,
            at=self._clock.now(),
            error=error,
            result_json=result_json,
        )
        return await self._commands.upsert(updated)
