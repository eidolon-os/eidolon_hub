"""Connection-plane domain values.

A connection proves device reachability and provides a signaling return path.
It is not a data/media channel.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class ConnectorKind(StrEnum):
    HTTPS = "https"
    MQTT5 = "mqtt5"
    EXPLICIT_URI = "explicit-uri"
    MDNS = "mdns"
    UNICAST_DNS_SD = "unicast-dns-sd"


class ConnectionState(StrEnum):
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
class ConnectionLease:
    connection_id: str
    device_id: str
    connector_id: str
    connector_kind: ConnectorKind
    signaling_ref: str
    opened_at: datetime
    renewed_at: datetime
    expires_at: datetime
    lease_token: str
    identity_fingerprint: str
    hub_instance_id: str
    fencing_token: int
    priority: int = 100
    heartbeat_sequence: int = 0
    state: ConnectionState = ConnectionState.ACTIVE

    def __post_init__(self) -> None:
        required = {
            "connection_id": self.connection_id,
            "device_id": self.device_id,
            "connector_id": self.connector_id,
            "signaling_ref": self.signaling_ref,
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
        return self.state is ConnectionState.ACTIVE and self.expires_at > effective_now

    def renew(
        self,
        *,
        now: datetime,
        ttl: timedelta,
        lease_token: str,
        sequence: int | None = None,
    ) -> "ConnectionLease":
        if self.state is ConnectionState.CLOSED:
            raise ValueError("closed connection cannot be renewed")
        if lease_token != self.lease_token:
            raise PermissionError("connection lease token mismatch")
        if ttl <= timedelta(0):
            raise ValueError("connection ttl must be positive")
        next_sequence = self.heartbeat_sequence + 1 if sequence is None else sequence
        if next_sequence < self.heartbeat_sequence:
            raise ValueError("stale connection heartbeat sequence")
        if next_sequence == self.heartbeat_sequence and self.heartbeat_sequence > 0:
            return self
        return replace(
            self,
            renewed_at=now,
            expires_at=now + ttl,
            heartbeat_sequence=next_sequence,
        )

    def close(self) -> "ConnectionLease":
        return replace(self, state=ConnectionState.CLOSED)
