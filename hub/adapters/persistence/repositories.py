"""SQLAlchemy implementations of Hub device and audit ports."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.adapters.persistence.channel_reconciliation import SqlChannelRevocationStore
from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.device_erase import SqlDeviceEraseLedger
from hub.adapters.persistence.models import (
    DeviceManagementEventRow,
    DeviceRow,
)
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import (
    DeviceManagementEventRecord,
    StoredDeviceManagementEvent,
)
from hub.ports.repositories import ConcurrentDeviceMutationError


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SqlDeviceRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, device_id: str) -> ManagedDevice | None:
        async with self._database.sessions() as session:
            row = await session.get(DeviceRow, device_id)
            return (
                None
                if row is None
                else self._decode(row, owner_domain_id=self._database.owner_domain_id)
            )

    async def list_all(self) -> tuple[ManagedDevice, ...]:
        async with self._database.sessions() as session:
            rows = (await session.scalars(select(DeviceRow).order_by(DeviceRow.device_id))).all()
            return tuple(
                self._decode(row, owner_domain_id=self._database.owner_domain_id) for row in rows
            )

    @staticmethod
    def _values(device: ManagedDevice) -> dict[str, object]:
        return {
            "device_id": device.identity.device_id,
            "display_name": device.display_name,
            "manifest_id": device.manifest_id,
            "manifest_json": device.manifest_json,
            "manifest_revision": device.manifest_digest,
            "manifest_declared_revision": device.manifest_declared_revision,
            "enrolled_at": device.enrolled_at,
            "updated_at": device.updated_at,
            "owner_domain_generation": device.owner_domain_generation,
            "claim_generation": device.claim_generation,
            "trust_epoch": device.trust_epoch,
            "aggregate_revision": device.aggregate_revision,
            "owner_id": device.owner_id,
            "lifecycle_state": device.lifecycle_state.value,
            "last_management_request_id": device.last_management_request_id,
            "last_management_fingerprint": device.last_management_fingerprint,
        }

    @classmethod
    async def _upsert(cls, session: AsyncSession, device: ManagedDevice) -> None:
        row = await session.get(DeviceRow, device.identity.device_id)
        values = cls._values(device)
        if row is None:
            session.add(DeviceRow(**values))
            return
        for name, value in values.items():
            setattr(row, name, value)

    @staticmethod
    def _decode(row: DeviceRow, *, owner_domain_id: str = "owner-test") -> ManagedDevice:
        return ManagedDevice(
            identity=DeviceIdentity(row.device_id),
            display_name=row.display_name,
            manifest_id=row.manifest_id,
            manifest=DeviceManifestDocument(
                canonical_json=row.manifest_json,
                digest=row.manifest_revision,
                declared_revision=row.manifest_declared_revision,
            ),
            enrolled_at=_aware(row.enrolled_at),
            updated_at=_aware(row.updated_at),
            owner_domain_generation=row.owner_domain_generation,
            owner_domain_id=owner_domain_id,
            claim_generation=row.claim_generation,
            trust_epoch=row.trust_epoch,
            aggregate_revision=row.aggregate_revision,
            owner_id=row.owner_id,
            lifecycle_state=DeviceLifecycleState(row.lifecycle_state),
            last_management_request_id=row.last_management_request_id,
            last_management_fingerprint=row.last_management_fingerprint,
        )


class SqlDeviceManagementEventLedger:
    """Durable management audit ledger; duplicate event IDs are idempotent."""

    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    @staticmethod
    def _data_json(event: DeviceManagementEventRecord) -> str:
        data_json = json.dumps(
            event.data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(data_json.encode()) > 256 * 1024:
            raise ValueError("management event data exceeds 256KiB")
        return data_json

    @classmethod
    async def _append(
        cls,
        session: AsyncSession,
        *,
        event: DeviceManagementEventRecord,
        owner_id: str,
        data_json: str | None = None,
    ) -> None:
        encoded = data_json if data_json is not None else cls._data_json(event)
        current = await session.scalar(
            select(DeviceManagementEventRow).where(
                DeviceManagementEventRow.event_id == event.event_id
            )
        )
        if current is not None:
            if (
                current.event_type != event.event_type
                or current.source != event.source
                or current.principal_id != event.principal_id
                or current.subject != event.subject
                or current.owner_id != owner_id
                or _aware(current.occurred_at) != event.occurred_at
                or current.data_json != encoded
            ):
                raise ValueError("event_id was reused with different content")
            return
        session.add(
            DeviceManagementEventRow(
                event_id=event.event_id,
                event_type=event.event_type,
                source=event.source,
                principal_id=event.principal_id,
                subject=event.subject,
                owner_id=owner_id,
                occurred_at=event.occurred_at,
                data_json=encoded,
            )
        )

    async def list_after(
        self, *, owner_scope: str, stream_position: int, limit: int
    ) -> tuple[StoredDeviceManagementEvent, ...]:
        if stream_position < 0:
            raise ValueError("stream_position must not be negative")
        if not 1 <= limit <= 500:
            raise ValueError("event stream limit must be between 1 and 500")
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(DeviceManagementEventRow)
                    .where(
                        DeviceManagementEventRow.stream_position > stream_position,
                        DeviceManagementEventRow.owner_id == owner_scope,
                    )
                    .order_by(DeviceManagementEventRow.stream_position)
                    .limit(limit)
                )
            ).all()
        return tuple(
            StoredDeviceManagementEvent(
                stream_position=row.stream_position,
                event=DeviceManagementEventRecord(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    source=row.source,
                    principal_id=row.principal_id,
                    subject=row.subject,
                    occurred_at=_aware(row.occurred_at),
                    data=json.loads(row.data_json),
                ),
            )
            for row in rows
        )


class SqlDeviceMutationUnitOfWork:
    """Single-process atomic boundary for device facts and audit events."""

    def __init__(self, database: HubDatabase, *, lock: asyncio.Lock | None = None) -> None:
        self._database = database
        self._lock = lock or asyncio.Lock()

    async def commit(
        self,
        *,
        expected: ManagedDevice | None,
        device: ManagedDevice,
        event: DeviceManagementEventRecord,
    ) -> ManagedDevice:
        if event.subject != device.identity.device_id:
            raise ValueError("management event subject must match device")
        data_json = SqlDeviceManagementEventLedger._data_json(event)
        owner_id = device.owner_id or "unclaimed"
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(DeviceRow, device.identity.device_id)
                actual = (
                    None
                    if row is None
                    else SqlDeviceRepository._decode(
                        row, owner_domain_id=self._database.owner_domain_id
                    )
                )
                if actual != expected:
                    raise ConcurrentDeviceMutationError(
                        "device changed concurrently; reload and retry"
                    )
                await SqlDeviceRepository._upsert(session, device)
                await SqlDeviceManagementEventLedger._append(
                    session,
                    event=event,
                    owner_id=owner_id,
                    data_json=data_json,
                )
        return device


class SqlHubRepositories:
    """Composition-only bundle; callers receive individual repository ports."""

    def __init__(self, database: HubDatabase) -> None:
        mutation_lock = asyncio.Lock()
        self.devices = SqlDeviceRepository(database)
        self.device_mutations = SqlDeviceMutationUnitOfWork(database, lock=mutation_lock)
        self.channel_revocations = SqlChannelRevocationStore(database)
        self.device_erase = SqlDeviceEraseLedger(database)
        self.management_events = SqlDeviceManagementEventLedger(database)
