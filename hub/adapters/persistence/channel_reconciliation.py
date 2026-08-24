"""SQLite projection of ClaimRevoked into retryable Channel revoke delivery."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from sqlalchemy import select

from hub.channel_reconciliation.domain import ChannelRevocationDelivery
from hub.contracts.bindings.admission import ClaimRevokedEvent
from hub.contracts.bindings.device import DeviceRef

from .database import HubDatabase
from .models import AdmissionClaimEventStreamRow, ChannelRevocationDeliveryRow


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _operation_id(event_id: str) -> str:
    return "channel-revoke-" + hashlib.sha256(event_id.encode()).hexdigest()[:48]


class SqlChannelRevocationStore:
    REVOKE_EVENT_TYPE = "live.eidolon.device.claim-revoked.v1"

    def __init__(self, database: HubDatabase, *, lock: asyncio.Lock | None = None) -> None:
        self._database = database
        self._lock = lock or asyncio.Lock()

    @staticmethod
    def _decode(row: ChannelRevocationDeliveryRow) -> ChannelRevocationDelivery:
        return ChannelRevocationDelivery(
            source_event_id=row.source_event_id,
            operation_id=row.operation_id,
            device_ref=DeviceRef(
                device_instance_id=row.device_id,
                owner_domain_id=row.owner_domain_id,
                owner_domain_generation=row.owner_domain_generation,
                claim_generation=row.claim_generation,
                trust_epoch=row.trust_epoch,
            ),
            reason=row.reason,
            state=row.state,
            attempt_count=row.attempt_count,
            next_attempt_at=_aware(row.next_attempt_at),
            delivered_at=None if row.delivered_at is None else _aware(row.delivered_at),
            result_code=row.result_code,
            last_error=row.last_error,
        )

    async def materialize_claim_events(self, *, now: datetime) -> int:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                events = (
                    await session.scalars(
                        select(AdmissionClaimEventStreamRow)
                        .where(AdmissionClaimEventStreamRow.event_type == self.REVOKE_EVENT_TYPE)
                        .order_by(AdmissionClaimEventStreamRow.stream_position)
                    )
                ).all()
                created = 0
                for row in events:
                    if await session.get(ChannelRevocationDeliveryRow, row.event_id) is not None:
                        continue
                    event = ClaimRevokedEvent.model_validate_json(row.event_json)
                    ref = event.data.device_ref
                    session.add(
                        ChannelRevocationDeliveryRow(
                            source_event_id=event.id,
                            operation_id=_operation_id(event.id),
                            device_id=ref.device_instance_id,
                            owner_domain_id=str(ref.owner_domain_id),
                            owner_domain_generation=ref.owner_domain_generation,
                            claim_generation=ref.claim_generation,
                            trust_epoch=ref.trust_epoch,
                            reason=event.data.reason,
                            state="pending",
                            attempt_count=0,
                            next_attempt_at=now,
                            delivered_at=None,
                            result_code="",
                            last_error="",
                        )
                    )
                    created += 1
                return created

    async def list_due(self, *, now: datetime, limit: int) -> tuple[ChannelRevocationDelivery, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Channel reconciliation batch limit must be 1..100")
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ChannelRevocationDeliveryRow)
                    .where(
                        ChannelRevocationDeliveryRow.state == "pending",
                        ChannelRevocationDeliveryRow.next_attempt_at <= now,
                    )
                    .order_by(ChannelRevocationDeliveryRow.next_attempt_at)
                    .limit(limit)
                )
            ).all()
        return tuple(self._decode(row) for row in rows)

    async def mark_terminal(
        self,
        *,
        source_event_id: str,
        state: str,
        result_code: str,
        delivered_at: datetime,
    ) -> None:
        if state not in {"delivered", "fenced"}:
            raise ValueError("Channel terminal delivery state must be delivered or fenced")
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(ChannelRevocationDeliveryRow, source_event_id)
                if row is None:
                    raise KeyError(source_event_id)
                if row.state in {"delivered", "fenced"}:
                    return
                row.state = state
                row.delivered_at = delivered_at
                row.result_code = result_code
                row.last_error = ""

    async def mark_retry(
        self,
        *,
        source_event_id: str,
        attempt_count: int,
        next_attempt_at: datetime,
        error: str,
    ) -> None:
        async with self._lock:
            async with self._database.sessions.begin() as session:
                row = await session.get(ChannelRevocationDeliveryRow, source_event_id)
                if row is None:
                    raise KeyError(source_event_id)
                if row.state != "pending":
                    return
                row.attempt_count = attempt_count
                row.next_attempt_at = next_attempt_at
                row.last_error = error[:512]
