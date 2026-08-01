from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.commands.state_machine import InvalidCommandTransition, transition_command


def _command(now: datetime) -> DeviceCommand:
    return DeviceCommand(
        command_id="command-1",
        device_id="device-1",
        operation="display.render",
        payload_json='{"text":"hello"}',
        state=CommandState.QUEUED,
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        updated_at=now,
    )


def test_command_happy_path_has_explicit_transitions() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    sent = transition_command(_command(now), CommandState.SENT, at=now)
    accepted = transition_command(sent, CommandState.ACCEPTED, at=now)
    succeeded = transition_command(
        accepted, CommandState.SUCCEEDED, at=now, result_json='{"ok":true}'
    )

    assert succeeded.terminal
    assert succeeded.result_json == '{"ok":true}'


def test_ttl_expiry_overrides_non_terminal_target() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    expired = transition_command(_command(now), CommandState.SENT, at=now + timedelta(seconds=31))

    assert expired.state is CommandState.EXPIRED
    assert expired.error == "command ttl expired"


def test_terminal_state_cannot_be_overwritten() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    failed = transition_command(_command(now), CommandState.FAILED, at=now)

    with pytest.raises(InvalidCommandTransition):
        transition_command(failed, CommandState.SUCCEEDED, at=now)


def test_every_command_state_pair_has_an_explicit_outcome() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    allowed = {
        (CommandState.QUEUED, CommandState.SENT),
        (CommandState.QUEUED, CommandState.EXPIRED),
        (CommandState.QUEUED, CommandState.FAILED),
        (CommandState.SENT, CommandState.ACCEPTED),
        (CommandState.SENT, CommandState.RUNNING),
        (CommandState.SENT, CommandState.SUCCEEDED),
        (CommandState.SENT, CommandState.FAILED),
        (CommandState.SENT, CommandState.REJECTED),
        (CommandState.SENT, CommandState.EXPIRED),
        (CommandState.ACCEPTED, CommandState.RUNNING),
        (CommandState.ACCEPTED, CommandState.SUCCEEDED),
        (CommandState.ACCEPTED, CommandState.FAILED),
        (CommandState.ACCEPTED, CommandState.REJECTED),
        (CommandState.ACCEPTED, CommandState.EXPIRED),
        (CommandState.RUNNING, CommandState.SUCCEEDED),
        (CommandState.RUNNING, CommandState.FAILED),
        (CommandState.RUNNING, CommandState.REJECTED),
        (CommandState.RUNNING, CommandState.EXPIRED),
    }

    for source in CommandState:
        command = replace(_command(now), state=source)
        for target in CommandState:
            if target is source:
                assert transition_command(command, target, at=now) is command
            elif (source, target) in allowed:
                assert transition_command(command, target, at=now).state is target
            else:
                with pytest.raises(InvalidCommandTransition):
                    transition_command(command, target, at=now)
