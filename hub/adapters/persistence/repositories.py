"""SQLAlchemy implementations of Hub device and audit ports."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import DeviceManagementEventRow, DeviceRow
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import (
    DeviceManagementEventRecord,
    StoredDeviceManagementEvent,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SqlDeviceRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, device_id: str) -> ManagedDevice | None:
        async with self._database.sessions() as session:
            row = await session.get(DeviceRow, device_id)
            return None if row is None else self._decode(row)

    async def get_by_enrollment_id(self, enrollment_id: str) -> ManagedDevice | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(DeviceRow).where(DeviceRow.enrollment_id == enrollment_id)
            )
            return None if row is None else self._decode(row)

    async def upsert(self, device: ManagedDevice) -> ManagedDevice:
        async with self._database.sessions.begin() as session:
            row = await session.get(DeviceRow, device.identity.device_id)
            values = self._values(device)
            if row is None:
                session.add(DeviceRow(**values))
            else:
                for name, value in values.items():
                    setattr(row, name, value)
        return device

    async def list_all(self) -> tuple[ManagedDevice, ...]:
        async with self._database.sessions() as session:
            rows = (await session.scalars(select(DeviceRow).order_by(DeviceRow.device_id))).all()
            return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _values(device: ManagedDevice) -> dict[str, object]:
        return {
            "device_id": device.identity.device_id,
            "enrollment_id": device.enrollment_id,
            "retrieval_token_hash": device.retrieval_token_hash,
            "retrieval_expires_at": device.retrieval_expires_at,
            "display_name": device.display_name,
            "device_kind": device.device_kind,
            "manifest_json": device.manifest_json,
            "manifest_revision": device.manifest_revision,
            "enrolled_at": device.enrolled_at,
            "updated_at": device.updated_at,
            "last_enrollment_request_id": device.last_enrollment_request_id,
            "last_enrollment_fingerprint": device.last_enrollment_fingerprint,
            "owner_id": device.owner_id,
            "lifecycle_state": device.lifecycle_state.value,
            "last_management_request_id": device.last_management_request_id,
            "last_management_fingerprint": device.last_management_fingerprint,
        }

    @staticmethod
    def _decode(row: DeviceRow) -> ManagedDevice:
        return ManagedDevice(
            identity=DeviceIdentity(row.device_id),
            enrollment_id=row.enrollment_id,
            retrieval_token_hash=row.retrieval_token_hash,
            retrieval_expires_at=_aware(row.retrieval_expires_at),
            display_name=row.display_name,
            device_kind=row.device_kind,
            manifest=DeviceManifestDocument(
                canonical_json=row.manifest_json,
                revision=row.manifest_revision,
            ),
            enrolled_at=_aware(row.enrolled_at),
            updated_at=_aware(row.updated_at),
            last_enrollment_request_id=row.last_enrollment_request_id,
            last_enrollment_fingerprint=row.last_enrollment_fingerprint,
            owner_id=row.owner_id,
            lifecycle_state=DeviceLifecycleState(row.lifecycle_state),
            last_management_request_id=row.last_management_request_id,
            last_management_fingerprint=row.last_management_fingerprint,
        )


class SqlDeviceManagementEventLedger:
    """Durable management audit ledger; duplicate event IDs are idempotent."""

    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def publish(self, event: DeviceManagementEventRecord) -> None:
        data_json = json.dumps(
            event.data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(data_json.encode()) > 256 * 1024:
            raise ValueError("management event data exceeds 256KiB")
        async with self._database.sessions.begin() as session:
            current = await session.scalar(
                select(DeviceManagementEventRow).where(
                    DeviceManagementEventRow.event_id == event.event_id
                )
            )
            if current is not None:
                if (
                    current.event_type != event.event_type
                    or current.subject != event.subject
                    or current.data_json != data_json
                ):
                    raise ValueError("event_id was reused with different content")
                return
            device = await session.get(DeviceRow, event.subject)
            session.add(
                DeviceManagementEventRow(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    source=event.source,
                    subject=event.subject,
                    owner_id=(device.owner_id if device and device.owner_id else "unclaimed"),
                    occurred_at=event.occurred_at,
                    data_json=data_json,
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
                    subject=row.subject,
                    occurred_at=_aware(row.occurred_at),
                    data=json.loads(row.data_json),
                ),
            )
            for row in rows
        )


class SqlHubRepositories:
    """Composition-only bundle; callers receive individual repository ports."""

    def __init__(self, database: HubDatabase) -> None:
        self.devices = SqlDeviceRepository(database)
        self.management_events = SqlDeviceManagementEventLedger(database)
