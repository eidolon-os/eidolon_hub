"""SQLAlchemy implementations of Hub device and audit ports."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.device_erase import SqlDeviceEraseLedger
from hub.adapters.persistence.models import (
    ClaimCommandResultRow,
    ClaimEventRow,
    DeviceControlOperationRow,
    DeviceManagementEventRow,
    DeviceRow,
)
from hub.domain.devices.entities import DeviceLifecycleState, DeviceRef, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.claim_lifecycle import (
    ClaimCommandResult,
    ClaimEventRecord,
    StoredClaimEvent,
)
from hub.ports.device_control import DeviceControlOperation
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
            return None if row is None else self._decode(row)

    async def get_by_enrollment_id(self, enrollment_id: str) -> ManagedDevice | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(DeviceRow).where(DeviceRow.enrollment_id == enrollment_id)
            )
            return None if row is None else self._decode(row)

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
            owner_domain_generation=row.owner_domain_generation,
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
                actual = None if row is None else SqlDeviceRepository._decode(row)
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


class SqlClaimLifecycleStore:
    """Atomic Claim command results and immutable Claim event stream."""

    COMMAND_TYPE = "device.claim.revoke"

    def __init__(self, database: HubDatabase, *, lock: asyncio.Lock | None = None) -> None:
        self._database = database
        self._lock = lock or asyncio.Lock()

    @staticmethod
    def _decode_command(row: ClaimCommandResultRow, *, replayed: bool = False) -> ClaimCommandResult:
        return ClaimCommandResult(
            command_id=row.command_id,
            fingerprint=row.fingerprint,
            outcome="replayed" if replayed else row.outcome,
            device_ref=DeviceRef(
                device_instance_id=row.device_id,
                owner_domain_id=row.owner_domain_id,
                owner_domain_generation=row.owner_domain_generation,
                claim_generation=row.claim_generation,
                trust_epoch=row.trust_epoch,
                accepted_manifest_digest=row.accepted_manifest_digest,
            ),
            aggregate_revision=row.aggregate_revision,
            occurred_at=_aware(row.occurred_at),
            event_id=row.event_id,
        )

    async def get_command(
        self, *, owner_domain_id: str, command_type: str, command_id: str
    ) -> ClaimCommandResult | None:
        async with self._database.sessions() as session:
            row = await session.get(
                ClaimCommandResultRow,
                (owner_domain_id, command_type, command_id),
            )
            return None if row is None else self._decode_command(row, replayed=True)

    async def commit_revoke(
        self,
        *,
        expected: ManagedDevice,
        revoked: ManagedDevice,
        command_id: str,
        fingerprint: str,
        event: ClaimEventRecord,
    ) -> ClaimCommandResult:
        device_ref = revoked.device_ref
        if device_ref is None or device_ref != event.device_ref:
            raise ValueError("revoked Claim event must match the device reference")
        async with self._lock:
            async with self._database.sessions.begin() as session:
                command_key = (
                    device_ref.owner_domain_id,
                    self.COMMAND_TYPE,
                    command_id,
                )
                existing = await session.get(ClaimCommandResultRow, command_key)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise ValueError("command_id was reused with different content")
                    return self._decode_command(existing, replayed=True)
                row = await session.get(DeviceRow, device_ref.device_instance_id)
                actual = None if row is None else SqlDeviceRepository._decode(row)
                if actual != expected:
                    raise ConcurrentDeviceMutationError(
                        "device changed concurrently; reload and retry"
                    )
                await SqlDeviceRepository._upsert(session, revoked)
                management_event = DeviceManagementEventRecord(
                    event_id=event.event_id,
                    event_type="eidolon.device.revoked.v1",
                    source="eidolon-hub/admission",
                    principal_id=event.actor_principal_id,
                    subject=device_ref.device_instance_id,
                    occurred_at=event.occurred_at,
                    data={
                        "reason": event.reason,
                        "claim_generation": device_ref.claim_generation,
                        "trust_epoch": device_ref.trust_epoch,
                        "aggregate_revision": event.aggregate_revision,
                        "correlation_id": event.correlation_id,
                        "causation_id": event.causation_id,
                    },
                )
                await SqlDeviceManagementEventLedger._append(
                    session,
                    event=management_event,
                    owner_id=device_ref.owner_domain_id,
                )
                session.add(
                    ClaimEventRow(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        device_id=device_ref.device_instance_id,
                        owner_domain_id=device_ref.owner_domain_id,
                        owner_domain_generation=device_ref.owner_domain_generation,
                        claim_generation=device_ref.claim_generation,
                        trust_epoch=device_ref.trust_epoch,
                        accepted_manifest_digest=device_ref.accepted_manifest_digest,
                        aggregate_revision=event.aggregate_revision,
                        correlation_id=event.correlation_id,
                        causation_id=event.causation_id,
                        actor_principal_id=event.actor_principal_id,
                        occurred_at=event.occurred_at,
                        reason=event.reason,
                    )
                )
                result_row = ClaimCommandResultRow(
                    owner_domain_id=device_ref.owner_domain_id,
                    command_type=self.COMMAND_TYPE,
                    command_id=command_id,
                    fingerprint=fingerprint,
                    outcome="committed",
                    device_id=device_ref.device_instance_id,
                    owner_domain_generation=device_ref.owner_domain_generation,
                    claim_generation=device_ref.claim_generation,
                    trust_epoch=device_ref.trust_epoch,
                    accepted_manifest_digest=device_ref.accepted_manifest_digest,
                    aggregate_revision=event.aggregate_revision,
                    occurred_at=event.occurred_at,
                    event_id=event.event_id,
                )
                session.add(result_row)
            return self._decode_command(result_row)

    async def commit_terminal_result(
        self,
        *,
        device: ManagedDevice,
        command_id: str,
        fingerprint: str,
        occurred_at: datetime,
    ) -> ClaimCommandResult:
        device_ref = device.device_ref
        if device_ref is None:
            raise ValueError("terminal Claim result requires an Owner-scoped device")
        async with self._lock:
            async with self._database.sessions.begin() as session:
                key = (device_ref.owner_domain_id, self.COMMAND_TYPE, command_id)
                existing = await session.get(ClaimCommandResultRow, key)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise ValueError("command_id was reused with different content")
                    return self._decode_command(existing, replayed=True)
                row = ClaimCommandResultRow(
                    owner_domain_id=device_ref.owner_domain_id,
                    command_type=self.COMMAND_TYPE,
                    command_id=command_id,
                    fingerprint=fingerprint,
                    outcome="committed",
                    device_id=device_ref.device_instance_id,
                    owner_domain_generation=device_ref.owner_domain_generation,
                    claim_generation=device_ref.claim_generation,
                    trust_epoch=device_ref.trust_epoch,
                    accepted_manifest_digest=device_ref.accepted_manifest_digest,
                    aggregate_revision=device.aggregate_revision,
                    occurred_at=occurred_at,
                    event_id=None,
                )
                session.add(row)
            return self._decode_command(row)

    async def list_events_after(
        self, *, stream_position: int, limit: int
    ) -> tuple[StoredClaimEvent, ...]:
        if stream_position < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid Claim event stream window")
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.stream_position > stream_position)
                    .order_by(ClaimEventRow.stream_position)
                    .limit(limit)
                )
            ).all()
        return tuple(
            StoredClaimEvent(
                stream_position=row.stream_position,
                event=ClaimEventRecord(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    device_ref=DeviceRef(
                        device_instance_id=row.device_id,
                        owner_domain_id=row.owner_domain_id,
                        owner_domain_generation=row.owner_domain_generation,
                        claim_generation=row.claim_generation,
                        trust_epoch=row.trust_epoch,
                        accepted_manifest_digest=row.accepted_manifest_digest,
                    ),
                    aggregate_revision=row.aggregate_revision,
                    correlation_id=row.correlation_id,
                    causation_id=row.causation_id,
                    actor_principal_id=row.actor_principal_id,
                    occurred_at=_aware(row.occurred_at),
                    reason=row.reason,
                ),
            )
            for row in rows
        )


class SqlDeviceControlStore:
    """Durable event-to-operation projection and retry state.

    Claim mutation transactions never depend on this store. If Hub stops after
    committing a Claim event, the next worker pass materializes the missing
    operation and resumes delivery.
    """

    REVOKE_EVENT_TYPE = "live.eidolon.device.claim-revoked.v1"
    REVOKE_OPERATION_TYPE = "channel.device-access.revoke"

    def __init__(self, database: HubDatabase, *, lock: asyncio.Lock | None = None) -> None:
        self._database = database
        self._lock = lock or asyncio.Lock()

    @staticmethod
    def _decode(row: DeviceControlOperationRow) -> DeviceControlOperation:
        return DeviceControlOperation(
            event_id=row.event_id,
            operation_type=row.operation_type,
            operation_id=row.operation_id,
            device_ref=DeviceRef(
                device_instance_id=row.device_id,
                owner_domain_id=row.owner_domain_id,
                owner_domain_generation=row.owner_domain_generation,
                claim_generation=row.claim_generation,
                trust_epoch=row.trust_epoch,
                accepted_manifest_digest=row.accepted_manifest_digest,
            ),
            reason=row.reason,
            state=row.state,
            attempt_count=row.attempt_count,
            next_attempt_at=_aware(row.next_attempt_at),
            delivered_at=(None if row.delivered_at is None else _aware(row.delivered_at)),
            last_error=row.last_error,
        )

    async def materialize_claim_events(self, *, now: datetime) -> int:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                events = (
                    await session.scalars(
                        select(ClaimEventRow)
                        .where(ClaimEventRow.event_type == self.REVOKE_EVENT_TYPE)
                        .order_by(ClaimEventRow.stream_position)
                    )
                ).all()
                created = 0
                for event in events:
                    if await session.get(DeviceControlOperationRow, event.event_id) is not None:
                        continue
                    session.add(
                        DeviceControlOperationRow(
                            event_id=event.event_id,
                            operation_type=self.REVOKE_OPERATION_TYPE,
                            operation_id=f"channel-revoke:{event.event_id}",
                            device_id=event.device_id,
                            owner_domain_id=event.owner_domain_id,
                            owner_domain_generation=event.owner_domain_generation,
                            claim_generation=event.claim_generation,
                            trust_epoch=event.trust_epoch,
                            accepted_manifest_digest=event.accepted_manifest_digest,
                            reason=event.reason,
                            state="pending",
                            attempt_count=0,
                            next_attempt_at=now,
                            delivered_at=None,
                            last_error="",
                        )
                    )
                    created += 1
                return created

    async def list_due(
        self, *, now: datetime, limit: int
    ) -> tuple[DeviceControlOperation, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Device Control batch limit must be between 1 and 100")
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(DeviceControlOperationRow)
                    .where(
                        DeviceControlOperationRow.state == "pending",
                        DeviceControlOperationRow.next_attempt_at <= now,
                    )
                    .order_by(DeviceControlOperationRow.next_attempt_at)
                    .limit(limit)
                )
            ).all()
        return tuple(self._decode(row) for row in rows)

    async def get_by_event_id(
        self, *, event_id: str
    ) -> DeviceControlOperation | None:
        async with self._database.sessions() as session:
            row = await session.get(DeviceControlOperationRow, event_id)
        return None if row is None else self._decode(row)

    async def mark_delivered(
        self, *, event_id: str, delivered_at: datetime
    ) -> None:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(DeviceControlOperationRow, event_id)
                if row is None:
                    raise KeyError(event_id)
                if row.state == "delivered":
                    return
                row.state = "delivered"
                row.delivered_at = delivered_at
                row.last_error = ""

    async def mark_retry(
        self,
        *,
        event_id: str,
        attempt_count: int,
        next_attempt_at: datetime,
        error: str,
    ) -> None:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(DeviceControlOperationRow, event_id)
                if row is None:
                    raise KeyError(event_id)
                if row.state == "delivered":
                    return
                row.attempt_count = attempt_count
                row.next_attempt_at = next_attempt_at
                row.last_error = error[:512]


class SqlHubRepositories:
    """Composition-only bundle; callers receive individual repository ports."""

    def __init__(self, database: HubDatabase) -> None:
        mutation_lock = asyncio.Lock()
        self.devices = SqlDeviceRepository(database)
        self.device_mutations = SqlDeviceMutationUnitOfWork(database, lock=mutation_lock)
        self.claim_lifecycle = SqlClaimLifecycleStore(database, lock=mutation_lock)
        self.device_control = SqlDeviceControlStore(database, lock=mutation_lock)
        self.device_erase = SqlDeviceEraseLedger(database)
        self.management_events = SqlDeviceManagementEventLedger(database)
