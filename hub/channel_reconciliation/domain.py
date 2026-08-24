"""Channel reconciliation values; Provider remains the Channel Authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from hub.contracts.bindings.device import DeviceRef


class ChannelBinding(BaseModel):
    """Provider-owned opaque binding exposed by Device Control configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=64)
    kinds: tuple[str, ...] = Field(min_length=1, max_length=8)
    binding_format: str = Field(min_length=1, max_length=128)
    issued_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(gt=0)
    opaque_binding: str = Field(min_length=1, max_length=131_072)


@dataclass(frozen=True, slots=True)
class ChannelRevocationDelivery:
    source_event_id: str
    operation_id: str
    device_ref: DeviceRef
    reason: str
    state: str
    attempt_count: int
    next_attempt_at: datetime
    delivered_at: datetime | None = None
    result_code: str = ""
    last_error: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"pending", "delivered", "fenced"}:
            raise ValueError("unsupported Channel reconciliation state")
        if self.attempt_count < 0:
            raise ValueError("Channel reconciliation attempt count must not be negative")
        if self.next_attempt_at.tzinfo is None:
            raise ValueError("Channel reconciliation retry time must be timezone-aware")
        if self.state in {"delivered", "fenced"} and self.delivered_at is None:
            raise ValueError("terminal Channel reconciliation requires evidence time")


class ChannelProviderError(RuntimeError):
    """Stable Provider failure, preserving domain code and retryability."""

    def __init__(self, code: str, *, retryable: bool, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.retryable = retryable


class ChannelProviderUnavailable(ChannelProviderError):
    def __init__(self, detail: str = "Channel Provider unavailable") -> None:
        super().__init__("PROVIDER_UNAVAILABLE", retryable=True, detail=detail)
