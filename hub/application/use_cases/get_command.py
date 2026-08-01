"""Read one durable command from the Hub command ledger."""

from __future__ import annotations

from hub.domain.commands.entities import DeviceCommand
from hub.ports.repositories import CommandRepository


class GetCommand:
    def __init__(self, commands: CommandRepository) -> None:
        self._commands = commands

    async def execute(self, command_id: str) -> DeviceCommand:
        command = await self._commands.get(command_id)
        if command is None:
            raise KeyError(command_id)
        return command
