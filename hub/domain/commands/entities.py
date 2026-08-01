"""Durable command values, separate from wire envelopes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class CommandState(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


TERMINAL_COMMAND_STATES = frozenset(
    {CommandState.SUCCEEDED, CommandState.FAILED, CommandState.REJECTED, CommandState.EXPIRED}
)


@dataclass(frozen=True, slots=True)
class DeviceCommand:
    command_id: str
    device_id: str
    operation: str
    payload_json: str
    state: CommandState
    created_at: datetime
    expires_at: datetime
    updated_at: datetime
    error: str = ""
    result_json: str | None = None

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.device_id.strip() or not self.operation.strip():
            raise ValueError("command_id, device_id and operation are required")
        if any(
            value.tzinfo is None for value in (self.created_at, self.expires_at, self.updated_at)
        ):
            raise ValueError("command timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("command must expire after it is created")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_COMMAND_STATES

    def with_state(
        self,
        state: CommandState,
        *,
        at: datetime,
        error: str = "",
        result_json: str | None = None,
    ) -> "DeviceCommand":
        return replace(self, state=state, updated_at=at, error=error, result_json=result_json)
