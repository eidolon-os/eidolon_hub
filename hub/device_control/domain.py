"""Device Control domain records for device-local.erase."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from hub.contracts.bindings.device import (
    DeviceLocalEraseCommand,
    DeviceLocalEraseOperationStatus,
)


class DeviceEraseState(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    DELIVERY_ACCEPTED = "delivery-accepted"
    ACKNOWLEDGED = "acknowledged"
    EXPIRED = "expired"
    PERMANENT_FAILURE = "permanent-failure"


class DeviceEraseIdempotencyConflict(ValueError):
    pass


class DeviceEraseGenerationConflict(ValueError):
    pass


class ManifestRevisionConflict(ValueError):
    """An assertion disagrees with the account of itself a device already gave.

    Either it is older than what the Authority accepted, or it reuses a revision
    for different content. Both mean the device and the Authority disagree about
    the device's own history, which no later assertion can repair silently.
    """


@dataclass(frozen=True, slots=True)
class DeviceEraseOperation:
    source_event_id: str
    command: DeviceLocalEraseCommand
    request_fingerprint: str
    public_key_spki: str | None
    key_id: str | None
    state: DeviceEraseState
    created_at: datetime
    attempt_count: int
    delivery_attempt_id: str | None = None
    delivery_accepted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    terminal_result: str | None = None
    result_code: str = ""
    last_error_code: str = ""

    @property
    def status(self) -> DeviceLocalEraseOperationStatus:
        return DeviceLocalEraseOperationStatus(
            operation_id=self.command.operation_id,
            request_fingerprint=self.request_fingerprint,
            device_ref=self.command.device_ref,
            created_at=self.created_at,
            deadline=self.command.deadline,
            state=self.state.value,
            attempt_count=self.attempt_count,
            terminal_result=self.terminal_result,
        )
