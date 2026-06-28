"""DeviceManager — SQLite-backed hub device registry."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from eidolon_sdk.biz.registry.models import DeviceRegistryRecord

from hub.core.device import Device

logger = logging.getLogger(__name__)


class DeviceManager:
    """Manage hub-owned device facts with an in-memory read cache.

    SQLite is the source of truth. The cache exists so existing request paths can
    keep cheap synchronous ``get`` / ``list_all`` calls while writes immediately
    persist through the SDK repository.
    """

    def __init__(self, repository):
        self._repository = repository
        self._devices: dict[str, Device] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        records = await self._repository.list_all()
        async with self._lock:
            self._devices = {
                device_id: _device_from_record(record)
                for device_id, record in records.items()
            }
        logger.info("Loaded %d devices from registry SQLite", len(self._devices))

    async def save(self) -> None:
        """Compatibility hook.

        Device mutations persist immediately, so shutdown-time save is a no-op.
        """
        return None

    def register(
        self,
        device_id: str,
        name: str = "",
        psk_hash: str | None = None,
        kind: str = "unknown",
    ) -> Device:
        """Register or touch a device in memory.

        Callers that need persistence must use an async method below.
        """
        if device_id in self._devices:
            device = self._devices[device_id]
            if name:
                device.name = name
            if kind and kind != "unknown":
                device.kind = kind
            if psk_hash:
                device.mark_paired(psk_hash)
            device.touch()
        else:
            device = Device(
                device_id=device_id,
                name=name,
                kind=kind,
                psk_hash=psk_hash,
                paired=psk_hash is not None,
            )
            self._devices[device_id] = device
            logger.info("Registered new device: %s (%s)", device_id, name or "unnamed")
        return device

    async def register_seen(
        self,
        device_id: str,
        name: str = "",
        psk_hash: str | None = None,
        metadata: dict | None = None,
    ) -> Device:
        """Register/touch a device observed through ``GET /api/config``."""
        async with self._lock:
            kind = str((metadata or {}).get("kind") or "unknown")
            device = self.register(
                device_id=device_id,
                name=name,
                psk_hash=psk_hash,
                kind=kind,
            )
            if metadata:
                device.metadata.update(metadata)
            await self._repository.put(_record_from_device(device))
            return device

    async def register_signed_seen(
        self,
        *,
        device_id: str,
        public_key: str,
        fingerprint: str,
        nonce: str,
        name: str = "",
        client_ip: str = "",
    ) -> Device:
        """Register/touch a signed device config request.

        Public key storage is TOFU: the first signed request locks the key for
        ``device_id``. Later requests may omit the key, but if they provide it
        the auth layer must already have confirmed it matches.
        """
        async with self._lock:
            device = self.register(device_id=device_id, name=name, kind="esp32")
            metadata = device.metadata
            if not metadata.get("public_key"):
                metadata["public_key"] = public_key
                metadata["fingerprint"] = fingerprint
            if client_ip:
                metadata["last_ip"] = client_ip
            recent = metadata.setdefault("recent_nonces", [])
            if nonce in recent:
                raise ValueError("replayed device nonce")
            recent.append(nonce)
            del recent[:-32]
            await self._repository.put(_record_from_device(device))
            return device

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def get_or_raise(self, device_id: str) -> Device:
        device = self.get(device_id)
        if device is None:
            raise KeyError(f"Device not found: {device_id}")
        return device

    def enable(self, device_id: str) -> Device:
        device = self.get_or_raise(device_id)
        device.enable()
        logger.info("Enabled device: %s", device_id)
        return device

    def disable(self, device_id: str) -> Device:
        device = self.get_or_raise(device_id)
        device.disable()
        logger.info("Disabled device: %s", device_id)
        return device

    async def set_enabled(self, device_id: str, *, enabled: bool) -> Device:
        async with self._lock:
            device = self.get_or_raise(device_id)
            previous = device.enabled
            if enabled:
                device.enable()
            else:
                device.disable()
            await self._repository.put(_record_from_device(device))
        if previous != enabled:
            logger.info("%s device: %s", "Enabled" if enabled else "Disabled", device_id)
        return device

    async def approve(self, device_id: str) -> Device:
        async with self._lock:
            device = self.get_or_raise(device_id)
            already = device.approved
            device.mark_approved()
            await self._repository.put(_record_from_device(device))
        if not already:
            logger.info("Approved device: %s", device_id)
        return device

    def list_all(self) -> list[Device]:
        return list(self._devices.values())

    def list_enabled(self) -> list[Device]:
        return [d for d in self._devices.values() if d.enabled]

    def list_paired(self) -> list[Device]:
        return [d for d in self._devices.values() if d.paired]

    def remove(self, device_id: str) -> None:
        if device_id in self._devices:
            del self._devices[device_id]
            logger.info("Removed device: %s", device_id)

    async def unregister(self, device_id: str) -> bool:
        async with self._lock:
            if device_id not in self._devices:
                return False
            del self._devices[device_id]
            await self._repository.delete(device_id)
        logger.info("Unregistered device: %s", device_id)
        return True

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._devices


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_optional_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return _parse_dt(value)


def _record_from_device(device: Device) -> DeviceRegistryRecord:
    return DeviceRegistryRecord(
        device_id=device.device_id,
        name=device.name,
        kind=device.kind,
        enabled=device.enabled,
        psk_hash=device.psk_hash,
        paired=device.paired,
        approved=device.approved,
        approved_at=_dt_to_iso(device.approved_at),
        created_at=_dt_to_iso(device.created_at) or _utc_now_iso(),
        last_seen=_dt_to_iso(device.last_seen) or _utc_now_iso(),
        metadata=device.metadata,
    )


def _device_from_record(record: DeviceRegistryRecord) -> Device:
    return Device(
        device_id=record.device_id,
        name=record.name,
        kind=record.kind,
        enabled=record.enabled,
        psk_hash=record.psk_hash,
        paired=record.paired,
        approved=record.approved,
        approved_at=_parse_optional_dt(record.approved_at),
        created_at=_parse_dt(record.created_at),
        last_seen=_parse_dt(record.last_seen),
        metadata=dict(record.metadata or {}),
    )
