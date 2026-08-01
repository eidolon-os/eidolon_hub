"""Protocol-independent aggregate for simultaneous device connections."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from threading import RLock

from hub.domain.connections.entities import ConnectionLease


class StaleFencingToken(RuntimeError):
    pass


class ConnectionNotFound(KeyError):
    pass


class ConnectionRegistry:
    """Tracks all return paths and derives online state from active leases."""

    def __init__(self) -> None:
        self._connections: dict[str, ConnectionLease] = {}
        self._by_device: dict[str, set[str]] = defaultdict(set)
        self._lock = RLock()

    def open(self, lease: ConnectionLease) -> ConnectionLease:
        with self._lock:
            current = self._connections.get(lease.connection_id)
            if current is not None:
                if current.device_id != lease.device_id:
                    raise ValueError("connection_id already belongs to another device")
                if lease.fencing_token < current.fencing_token:
                    raise StaleFencingToken(lease.connection_id)
                if lease.fencing_token == current.fencing_token:
                    return current
            self._connections[lease.connection_id] = lease
            self._by_device[lease.device_id].add(lease.connection_id)
            return lease

    def renew(
        self,
        connection_id: str,
        *,
        lease_token: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> ConnectionLease:
        effective_now = now or datetime.now(UTC)
        with self._lock:
            current = self._connections.get(connection_id)
            if current is None:
                raise ConnectionNotFound(connection_id)
            renewed = current.renew(now=effective_now, ttl=ttl, lease_token=lease_token)
            self._connections[connection_id] = renewed
            return renewed

    def close(self, connection_id: str) -> ConnectionLease:
        with self._lock:
            current = self._connections.get(connection_id)
            if current is None:
                raise ConnectionNotFound(connection_id)
            closed = current.close()
            self._connections[connection_id] = closed
            return closed

    def active_for_device(
        self, device_id: str, *, now: datetime | None = None
    ) -> tuple[ConnectionLease, ...]:
        effective_now = now or datetime.now(UTC)
        with self._lock:
            values = (
                self._connections[connection_id]
                for connection_id in self._by_device.get(device_id, ())
            )
            return tuple(
                sorted(
                    (item for item in values if item.is_active(effective_now)),
                    key=lambda item: (item.priority, -item.fencing_token, item.connection_id),
                )
            )

    def preferred_for_device(
        self, device_id: str, *, now: datetime | None = None
    ) -> ConnectionLease | None:
        active = self.active_for_device(device_id, now=now)
        return active[0] if active else None

    def is_online(self, device_id: str, *, now: datetime | None = None) -> bool:
        return bool(self.active_for_device(device_id, now=now))

    def expire(self, *, now: datetime | None = None) -> tuple[ConnectionLease, ...]:
        effective_now = now or datetime.now(UTC)
        with self._lock:
            expired = tuple(
                item
                for item in self._connections.values()
                if item.state.value == "active" and not item.is_active(effective_now)
            )
            for item in expired:
                self._connections[item.connection_id] = item.close()
            return expired
