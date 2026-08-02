"""Authenticated device-session and authority lease values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class DeviceSessionState(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class DeviceAuthorityLease:
    device_id: str
    hub_instance_id: str
    fencing_token: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.device_id or not self.hub_instance_id:
            raise ValueError("device_id and hub_instance_id are required")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if self.expires_at.tzinfo is None:
            raise ValueError("authority expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DeviceSessionLease:
    session_id: str
    device_id: str
    opened_at: datetime
    renewed_at: datetime
    expires_at: datetime
    lease_token: str
    identity_fingerprint: str
    hub_instance_id: str
    fencing_token: int
    heartbeat_sequence: int = 0
    state: DeviceSessionState = DeviceSessionState.ACTIVE

    def __post_init__(self) -> None:
        required = {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "lease_token": self.lease_token,
            "identity_fingerprint": self.identity_fingerprint,
            "hub_instance_id": self.hub_instance_id,
        }
        for field_name, value in required.items():
            if not value.strip():
                raise ValueError(f"{field_name} is required")
        for field_name in ("opened_at", "renewed_at", "expires_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if self.heartbeat_sequence < 0:
            raise ValueError("heartbeat_sequence must not be negative")
        if self.expires_at <= self.renewed_at:
            raise ValueError("expires_at must be after renewed_at")

    def is_active(self, now: datetime | None = None) -> bool:
        effective_now = now or datetime.now(UTC)
        return self.state is DeviceSessionState.ACTIVE and self.expires_at > effective_now

    def renew(
        self,
        *,
        now: datetime,
        ttl: timedelta,
        lease_token: str,
        sequence: int,
    ) -> "DeviceSessionLease":
        if self.state is DeviceSessionState.CLOSED:
            raise ValueError("closed session cannot be renewed")
        if lease_token != self.lease_token:
            raise PermissionError("session lease token mismatch")
        if ttl <= timedelta(0):
            raise ValueError("session ttl must be positive")
        if sequence < self.heartbeat_sequence:
            raise ValueError("stale session heartbeat sequence")
        if sequence < 1:
            raise ValueError("session heartbeat sequence must be positive")
        if sequence == self.heartbeat_sequence and self.heartbeat_sequence > 0:
            return self
        return replace(
            self,
            renewed_at=now,
            expires_at=now + ttl,
            heartbeat_sequence=sequence,
        )

    def close(self) -> "DeviceSessionLease":
        return replace(self, state=DeviceSessionState.CLOSED)
