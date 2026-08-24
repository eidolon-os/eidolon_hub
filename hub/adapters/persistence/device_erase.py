"""SQLite adapter for the independent device-local.erase ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    ClaimEventRow,
    DeviceEraseAckEvidenceRow,
    DeviceEraseOperationRow,
    DeviceOperationKeyBindingRow,
)
from hub.contracts.bindings.device import (
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceRef,
    operation_fingerprint,
)
from hub.device_control.domain import (
    DeviceEraseIdempotencyConflict,
    DeviceEraseOperation,
    DeviceEraseState,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _operation_id(event: ClaimEventRow) -> str:
    semantic = (
        f"device-local.erase\0{event.event_id}\0{event.device_id}\0"
        f"{event.owner_domain_id}\0{event.owner_domain_generation}\0"
        f"{event.claim_generation}\0{event.trust_epoch}\0"
        f"{event.accepted_manifest_digest}"
    )
    return "erase_" + hashlib.sha256(semantic.encode()).hexdigest()[:48]


class SqlDeviceEraseLedger:
    """One writer for erase Operations, delivery evidence and device ACKs."""

    REVOKE_EVENT_TYPE = "live.eidolon.device.claim-revoked.v1"

    def __init__(self, database: HubDatabase, *, lock: asyncio.Lock | None = None) -> None:
        self._database = database
        self._lock = lock or asyncio.Lock()

    @staticmethod
    def _decode(row: DeviceEraseOperationRow) -> DeviceEraseOperation:
        return DeviceEraseOperation(
            source_event_id=row.source_event_id,
            command=DeviceLocalEraseCommand.model_validate(json.loads(row.command_json)),
            request_fingerprint=row.request_fingerprint,
            public_key_spki=row.public_key_spki,
            key_id=row.key_id,
            state=DeviceEraseState(row.state),
            created_at=_aware(row.created_at),
            attempt_count=row.attempt_count,
            delivery_attempt_id=row.delivery_attempt_id,
            delivery_accepted_at=(
                None
                if row.delivery_accepted_at is None
                else _aware(row.delivery_accepted_at)
            ),
            acknowledged_at=(
                None if row.acknowledged_at is None else _aware(row.acknowledged_at)
            ),
            terminal_result=row.terminal_result,
            result_code=row.result_code,
            last_error_code=row.last_error_code,
        )

    async def bind_operation_key(
        self,
        *,
        enrollment_id: str,
        owner_domain_generation: int,
        claim_generation: int,
        proof,
        key_id: str,
        bound_at: datetime,
    ) -> None:
        key = (proof.device_instance_id, owner_domain_generation, claim_generation)
        async with self._lock:
            async with self._database.sessions.begin() as session:
                existing = await session.get(DeviceOperationKeyBindingRow, key)
                if existing is not None:
                    if (
                        existing.enrollment_id != enrollment_id
                        or existing.enrollment_request_id != proof.enrollment_request_id
                        or existing.public_key_spki != proof.public_key_spki
                        or existing.key_id != key_id
                    ):
                        raise DeviceEraseIdempotencyConflict(
                            "Claim generation operation key was rebound with different content"
                        )
                    return
                session.add(
                    DeviceOperationKeyBindingRow(
                        device_id=proof.device_instance_id,
                        owner_domain_generation=owner_domain_generation,
                        claim_generation=claim_generation,
                        enrollment_id=enrollment_id,
                        enrollment_request_id=proof.enrollment_request_id,
                        public_key_spki=proof.public_key_spki,
                        key_id=key_id,
                        bound_at=bound_at,
                    )
                )

    async def materialize_claim_events(
        self, *, now: datetime, operation_ttl: timedelta
    ) -> int:
        del now  # event time, never reconciliation time, defines the deadline
        if operation_ttl <= timedelta(0):
            raise ValueError("device erase operation TTL must be positive")
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
                    operation_id = _operation_id(event)
                    deadline = _aware(event.occurred_at) + operation_ttl
                    command = DeviceLocalEraseCommand(
                        operation_id=operation_id,
                        device_ref=DeviceRef(
                            device_instance_id=event.device_id,
                            owner_domain_id=event.owner_domain_id,
                            owner_domain_generation=event.owner_domain_generation,
                            claim_generation=event.claim_generation,
                            trust_epoch=event.trust_epoch,
                        ),
                        deadline=deadline,
                    )
                    fingerprint = operation_fingerprint(command)
                    existing = await session.get(DeviceEraseOperationRow, operation_id)
                    if existing is not None:
                        if existing.request_fingerprint != fingerprint:
                            raise DeviceEraseIdempotencyConflict(
                                "operation_id was reused with different content"
                            )
                        continue
                    by_event = await session.scalar(
                        select(DeviceEraseOperationRow).where(
                            DeviceEraseOperationRow.source_event_id == event.event_id
                        )
                    )
                    if by_event is not None:
                        if by_event.request_fingerprint != fingerprint:
                            raise DeviceEraseIdempotencyConflict(
                                "Claim event was projected with different erase content"
                            )
                        continue
                    binding = await session.get(
                        DeviceOperationKeyBindingRow,
                        (
                            event.device_id,
                            event.owner_domain_generation,
                            event.claim_generation,
                        ),
                    )
                    has_key = binding is not None
                    session.add(
                        DeviceEraseOperationRow(
                            operation_id=operation_id,
                            source_event_id=event.event_id,
                            request_fingerprint=fingerprint,
                            device_id=event.device_id,
                            owner_domain_id=event.owner_domain_id,
                            owner_domain_generation=event.owner_domain_generation,
                            claim_generation=event.claim_generation,
                            trust_epoch=event.trust_epoch,
                            accepted_manifest_digest=event.accepted_manifest_digest,
                            public_key_spki=(binding.public_key_spki if binding else None),
                            key_id=(binding.key_id if binding else None),
                            command_json=json.dumps(
                                command.model_dump(mode="json"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            state=(
                                DeviceEraseState.ACCEPTED.value
                                if has_key
                                else DeviceEraseState.PERMANENT_FAILURE.value
                            ),
                            created_at=_aware(event.occurred_at),
                            deadline=deadline,
                            attempt_count=0,
                            terminal_result=(None if has_key else "permanent-failure"),
                            result_code=("" if has_key else "ACK_KEY_NOT_BOUND"),
                            last_error_code=("" if has_key else "ACK_KEY_NOT_BOUND"),
                        )
                    )
                    created += 1
                return created

    async def expire_due(self, *, now: datetime) -> int:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                rows = (
                    await session.scalars(
                        select(DeviceEraseOperationRow).where(
                            DeviceEraseOperationRow.state.in_(
                                (
                                    DeviceEraseState.ACCEPTED.value,
                                    DeviceEraseState.PENDING.value,
                                    DeviceEraseState.DELIVERY_ACCEPTED.value,
                                )
                            ),
                            DeviceEraseOperationRow.deadline <= now,
                        )
                    )
                ).all()
                for row in rows:
                    row.state = DeviceEraseState.EXPIRED.value
                    row.terminal_result = "deadline-expired"
                    row.result_code = "OPERATION_EXPIRED"
                return len(rows)

    async def mark_accepted_pending(self, *, now: datetime) -> int:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                rows = (
                    await session.scalars(
                        select(DeviceEraseOperationRow).where(
                            DeviceEraseOperationRow.state
                            == DeviceEraseState.ACCEPTED.value,
                            DeviceEraseOperationRow.deadline > now,
                        )
                    )
                ).all()
                for row in rows:
                    row.state = DeviceEraseState.PENDING.value
                return len(rows)

    async def get(self, *, operation_id: str) -> DeviceEraseOperation | None:
        async with self._database.sessions() as session:
            row = await session.get(DeviceEraseOperationRow, operation_id)
        return None if row is None else self._decode(row)

    async def get_for_device(self, *, device_ref: DeviceRef) -> DeviceEraseOperation | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(DeviceEraseOperationRow).where(
                    DeviceEraseOperationRow.device_id == device_ref.device_instance_id,
                    DeviceEraseOperationRow.owner_domain_id == str(device_ref.owner_domain_id),
                    DeviceEraseOperationRow.owner_domain_generation
                    == device_ref.owner_domain_generation,
                    DeviceEraseOperationRow.claim_generation == device_ref.claim_generation,
                    DeviceEraseOperationRow.trust_epoch == device_ref.trust_epoch,
                )
            )
        return None if row is None else self._decode(row)

    async def operation_key_for(
        self, *, device_ref: DeviceRef
    ) -> tuple[str, str] | None:
        async with self._database.sessions() as session:
            binding = await session.get(
                DeviceOperationKeyBindingRow,
                (
                    device_ref.device_instance_id,
                    device_ref.owner_domain_generation,
                    device_ref.claim_generation,
                ),
            )
        if binding is None:
            return None
        return binding.public_key_spki, binding.key_id

    async def accept_delivery(
        self,
        *,
        operation_id: str,
        delivery_attempt_id: str,
        accepted_at: datetime,
    ) -> DeviceEraseOperation:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(DeviceEraseOperationRow, operation_id)
                if row is None:
                    raise KeyError(operation_id)
                if _aware(row.deadline) <= accepted_at:
                    if row.state not in {
                        DeviceEraseState.ACKNOWLEDGED.value,
                        DeviceEraseState.PERMANENT_FAILURE.value,
                    }:
                        row.state = DeviceEraseState.EXPIRED.value
                        row.terminal_result = "deadline-expired"
                        row.result_code = "OPERATION_EXPIRED"
                    return self._decode(row)
                if row.state in {
                    DeviceEraseState.ACKNOWLEDGED.value,
                    DeviceEraseState.EXPIRED.value,
                    DeviceEraseState.PERMANENT_FAILURE.value,
                }:
                    return self._decode(row)
                if row.delivery_attempt_id != delivery_attempt_id:
                    row.attempt_count += 1
                row.delivery_attempt_id = delivery_attempt_id
                row.delivery_accepted_at = accepted_at
                row.state = DeviceEraseState.DELIVERY_ACCEPTED.value
                return self._decode(row)

    async def apply_ack(
        self,
        *,
        ack: DeviceLocalEraseAck,
        ack_fingerprint: str,
        received_at: datetime,
    ) -> DeviceEraseOperation:
        evidence_key = (ack.operation_id, ack.ack_sequence)
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(DeviceEraseOperationRow, ack.operation_id)
                if row is None:
                    raise KeyError(ack.operation_id)
                existing = await session.get(DeviceEraseAckEvidenceRow, evidence_key)
                if existing is not None:
                    if existing.ack_fingerprint != ack_fingerprint:
                        raise DeviceEraseIdempotencyConflict(
                            "ack_sequence was reused with different content"
                        )
                    return self._decode(row)
                terminal_before_ack = row.state in {
                    DeviceEraseState.ACKNOWLEDGED.value,
                    DeviceEraseState.EXPIRED.value,
                    DeviceEraseState.PERMANENT_FAILURE.value,
                }
                if not terminal_before_ack and _aware(row.deadline) <= received_at:
                    row.state = DeviceEraseState.EXPIRED.value
                    row.terminal_result = "deadline-expired"
                    row.result_code = "OPERATION_EXPIRED"
                    terminal_before_ack = True
                applied = not terminal_before_ack
                if applied:
                    row.acknowledged_at = received_at
                    row.terminal_result = ack.result
                    row.result_code = ack.result_code
                    row.state = (
                        DeviceEraseState.ACKNOWLEDGED.value
                        if ack.result == "erased"
                        else DeviceEraseState.PERMANENT_FAILURE.value
                    )
                session.add(
                    DeviceEraseAckEvidenceRow(
                        operation_id=ack.operation_id,
                        ack_sequence=ack.ack_sequence,
                        ack_fingerprint=ack_fingerprint,
                        received_at=received_at,
                        applied=1 if applied else 0,
                        result=ack.result,
                        result_code=ack.result_code,
                    )
                )
                return self._decode(row)
