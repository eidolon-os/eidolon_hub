"""Explicit command lifecycle and TTL enforcement."""

from __future__ import annotations

from datetime import datetime

from hub.domain.commands.entities import CommandState, DeviceCommand

_ALLOWED: dict[CommandState, frozenset[CommandState]] = {
    CommandState.QUEUED: frozenset({CommandState.SENT, CommandState.EXPIRED, CommandState.FAILED}),
    CommandState.SENT: frozenset(
        {
            CommandState.ACCEPTED,
            CommandState.RUNNING,
            CommandState.SUCCEEDED,
            CommandState.FAILED,
            CommandState.REJECTED,
            CommandState.EXPIRED,
        }
    ),
    CommandState.ACCEPTED: frozenset(
        {
            CommandState.RUNNING,
            CommandState.SUCCEEDED,
            CommandState.FAILED,
            CommandState.REJECTED,
            CommandState.EXPIRED,
        }
    ),
    CommandState.RUNNING: frozenset(
        {CommandState.SUCCEEDED, CommandState.FAILED, CommandState.REJECTED, CommandState.EXPIRED}
    ),
    CommandState.SUCCEEDED: frozenset(),
    CommandState.FAILED: frozenset(),
    CommandState.REJECTED: frozenset(),
    CommandState.EXPIRED: frozenset(),
}


class InvalidCommandTransition(RuntimeError):
    pass


def transition_command(
    command: DeviceCommand,
    target: CommandState,
    *,
    at: datetime,
    error: str = "",
    result_json: str | None = None,
) -> DeviceCommand:
    if target == command.state:
        return command
    if command.expires_at <= at and not command.terminal:
        target = CommandState.EXPIRED
        error = error or "command ttl expired"
    if target not in _ALLOWED[command.state]:
        raise InvalidCommandTransition(f"{command.state.value} -> {target.value}")
    return command.with_state(target, at=at, error=error, result_json=result_json)
