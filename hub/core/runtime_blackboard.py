"""NATS KV-backed, owner-isolated runtime device blackboard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from eidolon_sdk.biz.body import (
    CapabilityDeclaration,
    CapabilityManifest,
    OwnerDeviceBlackboardSnapshot,
    RuntimeDeviceEntry,
    capability_manifest_revision,
    owner_device_blackboard_key,
)


class RuntimeBlackboardError(Exception):
    pass


class RuntimeCapabilityUnavailable(RuntimeBlackboardError):
    pass


class RuntimeBlackboardKV(Protocol):
    async def clear(self) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, value: bytes) -> object: ...

    async def delete(self, key: str) -> None: ...

    async def keys(self, prefix: str = "") -> list[str]: ...


class _MemoryKV:
    """Test backend; production always injects the JetStream KV adapter."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def clear(self) -> None:
        self.values.clear()

    async def put(self, key: str, value: bytes) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.values if key.startswith(prefix))


class OwnerRuntimeBlackboard:
    """One complete ``current`` snapshot per owner in JetStream KV.

    Hub is the only writer. Every mutation reads and replaces the owner's full
    snapshot under one process lock; Agent and command authorization read the
    same owner key directly. No device availability is restored from SQLite.
    """

    def __init__(
        self,
        kv: RuntimeBlackboardKV | None = None,
        *,
        epoch: str | None = None,
        lease_seconds: int = 45,
    ) -> None:
        self._kv = kv or _MemoryKV()
        self._epoch = epoch or f"epoch_{uuid4().hex}"
        self._lease_seconds = lease_seconds
        self._ready = True if kv is None else False
        self._lock = asyncio.Lock()

    @property
    def epoch(self) -> str:
        return self._epoch

    async def initialize(self, owner_ids: list[str]) -> None:
        """Clear every prior device snapshot and seed this Hub generation."""
        async with self._lock:
            await self._kv.clear()
            self._ready = False
            for owner_id in dict.fromkeys(owner_ids):
                await self._write(self._empty_snapshot(owner_id, ready=False))

    async def mark_ready(self, owner_ids: list[str]) -> None:
        async with self._lock:
            self._ready = True
            for owner_id in dict.fromkeys(owner_ids):
                current = await self._load(owner_id)
                await self._write(self._next_snapshot(current, ready=True))

    async def register_device_manifest(
        self,
        *,
        device_id: str,
        manifest: CapabilityManifest,
        owner_id: str | None,
        provider_companion_id: str | None,
        name: str,
        aliases: tuple[str, ...] = (),
        provider_companion_name: str = "",
        visibility: str = "owner",
        registration_id: str | None = None,
        registered_at: datetime | None = None,
    ) -> RuntimeDeviceEntry:
        if visibility not in {"owner", "bound_companion"}:
            raise ValueError(f"unsupported capability visibility: {visibility}")
        now = registered_at or datetime.now(UTC)
        lease_now = datetime.now(UTC)
        entry = RuntimeDeviceEntry(
            device_id=device_id,
            registration_id=registration_id or f"reg_{uuid4().hex}",
            provider_companion_id=provider_companion_id,
            provider_companion_name=provider_companion_name,
            name=name or device_id,
            aliases=tuple(dict.fromkeys(item.strip() for item in aliases if item.strip())),
            visibility=visibility,
            capabilities=manifest.capabilities,
            manifest_revision=capability_manifest_revision(manifest),
            status="registered_waiting_transport",
            registered_at=now,
            lease_expires_at=self._lease_deadline(lease_now),
        )
        # Pending/unowned devices receive a registration generation but never
        # enter an owner's capability blackboard.
        if not owner_id:
            return entry
        async with self._lock:
            snapshot = await self._load(owner_id)
            current = snapshot.devices.get(device_id)
            if current is not None and current.is_online(now=lease_now):
                # A signed manifest refresh and a LiveKit presence probe are
                # independent observations about the same physical device.
                # Refreshing the manifest must not make an already reachable
                # transport disappear while it reconnects with a newer token.
                entry = entry.model_copy(
                    update={
                        "status": "online",
                        "room_name": current.room_name,
                        "participant_sid": current.participant_sid,
                        "presence_revision": current.presence_revision,
                        "last_seen_at": current.last_seen_at,
                        "lease_expires_at": current.lease_expires_at,
                    }
                )
            devices = dict(snapshot.devices)
            devices[device_id] = entry
            await self._write(self._next_snapshot(snapshot, devices=devices))
        return entry

    async def mark_device_online(
        self,
        *,
        owner_id: str,
        device_id: str,
        room_name: str,
        participant_sid: str,
        presence_revision: str,
        seen_at: datetime | None = None,
    ) -> RuntimeDeviceEntry | None:
        now = seen_at or datetime.now(UTC)
        async with self._lock:
            snapshot = await self._load(owner_id)
            current = snapshot.devices.get(device_id)
            if current is None:
                return None
            updated = current.model_copy(
                update={
                    "status": "online",
                    "room_name": room_name,
                    "participant_sid": participant_sid,
                    "presence_revision": presence_revision,
                    "last_seen_at": now,
                    "lease_expires_at": self._lease_deadline(now),
                }
            )
            devices = dict(snapshot.devices)
            devices[device_id] = updated
            await self._write(self._next_snapshot(snapshot, devices=devices))
            return updated

    async def remove_device_session(
        self,
        *,
        owner_id: str | None,
        device_id: str,
    ) -> bool:
        if not owner_id:
            return False
        async with self._lock:
            snapshot = await self._load(owner_id)
            current = snapshot.devices.get(device_id)
            if current is None:
                return False
            devices = dict(snapshot.devices)
            del devices[device_id]
            await self._write(self._next_snapshot(snapshot, devices=devices))
            return True

    async def get_device(
        self, *, owner_id: str | None, device_id: str
    ) -> RuntimeDeviceEntry | None:
        if not owner_id:
            return None
        snapshot = await self.read_owner_snapshot(owner_id)
        return snapshot.devices.get(device_id) if snapshot is not None else None

    async def read_owner_snapshot(self, owner_id: str) -> OwnerDeviceBlackboardSnapshot | None:
        raw = await self._kv.get(owner_device_blackboard_key(owner_id))
        if raw is None:
            return None
        return OwnerDeviceBlackboardSnapshot.from_bytes(raw, expected_owner_id=owner_id)

    async def list_visible_runtime_devices(
        self,
        *,
        owner_id: str,
        requester_companion_id: str,
    ) -> list[RuntimeDeviceEntry]:
        snapshot = await self.read_owner_snapshot(owner_id)
        if snapshot is None:
            return []
        return snapshot.visible_devices(requester_companion_id=requester_companion_id)

    async def list_online_devices_for_owner(self, *, owner_id: str) -> list[RuntimeDeviceEntry]:
        snapshot = await self.read_owner_snapshot(owner_id)
        if snapshot is None or not snapshot.is_available():
            return []
        return sorted(
            (entry for entry in snapshot.devices.values() if entry.is_online()),
            key=lambda item: item.device_id,
        )

    async def resolve_current_capability(
        self,
        *,
        owner_id: str,
        requester_companion_id: str,
        device_id: str,
        capability_name: str,
        capability_version: int,
    ) -> tuple[RuntimeDeviceEntry, CapabilityDeclaration]:
        snapshot = await self.read_owner_snapshot(owner_id)
        if snapshot is None or not snapshot.is_available():
            raise RuntimeCapabilityUnavailable("runtime device blackboard is unavailable")
        entry = snapshot.devices.get(device_id)
        if entry is None or not entry.is_online():
            raise RuntimeCapabilityUnavailable(f"device {device_id!r} is not online")
        if (
            entry.visibility == "bound_companion"
            and entry.provider_companion_id != requester_companion_id
        ):
            raise RuntimeCapabilityUnavailable("device capabilities are private")
        capability = entry.capability(capability_name, capability_version)
        if capability is None:
            raise RuntimeCapabilityUnavailable(
                f"device {device_id!r} does not currently declare "
                f"{capability_name!r}.v{capability_version}"
            )
        return entry, capability

    async def _load(self, owner_id: str) -> OwnerDeviceBlackboardSnapshot:
        snapshot = await self.read_owner_snapshot(owner_id)
        if snapshot is None or snapshot.epoch != self._epoch:
            return self._empty_snapshot(owner_id, ready=self._ready)
        return snapshot

    async def _write(self, snapshot: OwnerDeviceBlackboardSnapshot) -> None:
        await self._kv.put(
            owner_device_blackboard_key(snapshot.owner_id),
            snapshot.to_bytes(),
        )

    def _empty_snapshot(self, owner_id: str, *, ready: bool) -> OwnerDeviceBlackboardSnapshot:
        now = datetime.now(UTC)
        return OwnerDeviceBlackboardSnapshot(
            owner_id=owner_id,
            epoch=self._epoch,
            revision=1,
            ready=ready,
            hub_lease_expires_at=self._lease_deadline(now),
            updated_at=now,
            devices={},
        )

    def _next_snapshot(
        self,
        snapshot: OwnerDeviceBlackboardSnapshot,
        *,
        devices: dict[str, RuntimeDeviceEntry] | None = None,
        ready: bool | None = None,
    ) -> OwnerDeviceBlackboardSnapshot:
        now = datetime.now(UTC)
        return snapshot.model_copy(
            update={
                "epoch": self._epoch,
                "revision": snapshot.revision + 1,
                "ready": self._ready if ready is None else ready,
                "hub_lease_expires_at": self._lease_deadline(now),
                "updated_at": now,
                "devices": snapshot.devices if devices is None else devices,
            }
        )

    def _lease_deadline(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self._lease_seconds)


__all__ = [
    "OwnerRuntimeBlackboard",
    "RuntimeBlackboardError",
    "RuntimeCapabilityUnavailable",
]
