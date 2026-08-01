"""Create a durable command and send it through a data channel, never MQTT."""

from __future__ import annotations

import json
from datetime import timedelta

from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.commands.state_machine import transition_command
from hub.ports.channels import CommandChannelSender
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import CommandRepository, DeviceRepository


class SendCommand:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        commands: CommandRepository,
        sender: CommandChannelSender,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._devices = devices
        self._commands = commands
        self._sender = sender
        self._clock = clock
        self._ids = ids

    async def execute(
        self,
        *,
        device_id: str,
        operation: str,
        payload_json: str,
        ttl: timedelta = timedelta(seconds=30),
        request_id: str | None = None,
    ) -> DeviceCommand:
        device = await self._devices.get(device_id)
        if device is None or device.revoked:
            raise KeyError(device_id)
        if not device.approved:
            raise PermissionError("device is not approved for commands")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("command payload_json must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("command payload_json must contain a JSON object")
        if ttl <= timedelta(0) or ttl > timedelta(minutes=5):
            raise ValueError("command ttl must be between zero and five minutes")
        now = self._clock.now()
        command_id = request_id or self._ids.new("command")
        current = await self._commands.get(command_id)
        if current is not None:
            same_request = (
                current.device_id == device_id
                and current.operation == operation
                and current.payload_json == payload_json
                and current.expires_at - current.created_at == ttl
            )
            if not same_request:
                raise ValueError("command request_id was reused with different content")
            if current.state is not CommandState.QUEUED:
                return current
        command = DeviceCommand(
            command_id=command_id,
            device_id=device_id,
            operation=operation,
            payload_json=payload_json,
            state=CommandState.QUEUED,
            created_at=now,
            expires_at=now + ttl,
            updated_at=now,
        )
        await self._commands.upsert(command)
        try:
            await self._sender.send_command(command)
        except Exception as exc:
            failed = transition_command(
                command, CommandState.FAILED, at=self._clock.now(), error=str(exc)
            )
            await self._commands.upsert(failed)
            raise
        sent = transition_command(command, CommandState.SENT, at=self._clock.now())
        return await self._commands.upsert(sent)
