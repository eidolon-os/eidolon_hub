"""Transactional persistence and outbox primitives for canonical Admission."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import func, select

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    AdmissionClaimRow,
    AdmissionCommandResultRow,
    AdmissionDecisionRow,
    AdmissionGrantAckRow,
    AdmissionGrantRow,
    AdmissionOutboxRow,
    AdmissionProposalRow,
)
from hub.admission.domain import AdmissionProblem


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SqlAdmissionStore:
    """The only transaction/session factory canonical Admission may use."""

    def __init__(self, database: HubDatabase, *, lock: asyncio.Lock | None = None) -> None:
        self.database = database
        self.lock = lock or asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        """Keep ORM/session ownership inside the Admission persistence boundary."""

        async with self.database.sessions.begin() as session:
            yield session

    async def get_claim(self, session, device_instance_id: str):
        return await session.get(AdmissionClaimRow, device_instance_id)

    async def get_decision(self, session, decision_id: str):
        return await session.get(AdmissionDecisionRow, decision_id)

    async def get_grant(self, session, grant_id: str):
        return await session.get(AdmissionGrantRow, grant_id)

    async def get_proposal(self, session, enrollment_id: str):
        return await session.get(AdmissionProposalRow, enrollment_id)

    async def get_grant_for_enrollment(self, session, enrollment_id: str):
        return await session.scalar(
            select(AdmissionGrantRow).where(AdmissionGrantRow.enrollment_id == enrollment_id)
        )

    async def get_grant_for_device_ref(self, session, device_ref_json: str):
        return await session.scalar(
            select(AdmissionGrantRow).where(AdmissionGrantRow.device_ref_json == device_ref_json)
        )

    async def get_grant_for_decision(self, session, decision_id: str):
        return await session.scalar(
            select(AdmissionGrantRow).where(AdmissionGrantRow.decision_id == decision_id)
        )

    async def list_expirable(self, session, *, states: tuple[str, ...], deadline: datetime):
        return (
            await session.scalars(
                select(AdmissionProposalRow)
                .where(
                    AdmissionProposalRow.state.in_(states),
                    AdmissionProposalRow.expires_at <= deadline,
                )
                .order_by(AdmissionProposalRow.enrollment_id)
            )
        ).all()

    async def max_claim_generation(
        self, session, *, owner_domain_id: str, hardware_identity_ref: str
    ) -> int:
        return (
            await session.scalar(
                select(func.max(AdmissionGrantRow.claim_generation)).where(
                    AdmissionGrantRow.owner_domain_id == owner_domain_id,
                    AdmissionGrantRow.hardware_identity_ref == hardware_identity_ref,
                )
            )
            or 0
        )

    @staticmethod
    def add_proposal(session, **values) -> None:
        session.add(AdmissionProposalRow(**values))

    @staticmethod
    def add_decision(session, **values) -> None:
        session.add(AdmissionDecisionRow(**values))

    @staticmethod
    def add_grant(session, **values) -> None:
        session.add(AdmissionGrantRow(**values))

    @staticmethod
    def add_grant_ack(session, **values) -> None:
        session.add(AdmissionGrantAckRow(**values))

    @staticmethod
    def add_claim(session, **values) -> None:
        session.add(AdmissionClaimRow(**values))

    async def load_claim(self, *, device_instance_id: str):
        async with self.database.sessions() as session:
            return await self.get_claim(session, device_instance_id)

    async def replay(
        self, *, owner_domain_id: str, command_type: str, command_id: str, fingerprint: str
    ) -> dict | None:
        async with self.database.sessions() as session:
            return await self.replay_in_session(
                session,
                owner_domain_id=owner_domain_id,
                command_type=command_type,
                command_id=command_id,
                fingerprint=fingerprint,
            )

    @staticmethod
    async def replay_in_session(
        session,
        *,
        owner_domain_id: str,
        command_type: str,
        command_id: str,
        fingerprint: str,
    ) -> dict | None:
        row = await session.get(
            AdmissionCommandResultRow, (owner_domain_id, command_type, command_id)
        )
        if row is None:
            return None
        if row.fingerprint != fingerprint:
            raise AdmissionProblem(
                "IDEMPOTENCY_CONFLICT", "command_id was reused with different canonical payload"
            )
        result = json.loads(row.result_json)
        result["outcome"] = "replayed"
        return result

    @staticmethod
    async def save_result(
        session,
        *,
        owner_domain_id: str,
        command_type: str,
        command_id: str,
        fingerprint: str,
        result: dict,
        occurred_at: datetime,
    ) -> None:
        session.add(
            AdmissionCommandResultRow(
                owner_domain_id=owner_domain_id,
                command_type=command_type,
                command_id=command_id,
                fingerprint=fingerprint,
                result_json=json.dumps(result, sort_keys=True, separators=(",", ":")),
                occurred_at=occurred_at,
            )
        )

    @staticmethod
    def add_outbox(
        session,
        *,
        event_id: str,
        event_type: str,
        aggregate_id: str,
        aggregate_revision: int,
        event: dict,
        occurred_at: datetime,
    ) -> None:
        session.add(
            AdmissionOutboxRow(
                event_id=event_id,
                event_type=event_type,
                source="urn:eidolon:authority:admission",
                aggregate_id=aggregate_id,
                aggregate_revision=aggregate_revision,
                event_json=json.dumps(event, sort_keys=True, separators=(",", ":")),
                occurred_at=occurred_at,
                published_at=None,
                publish_attempts=0,
                last_error="",
            )
        )

    async def pending_outbox(self, *, limit: int = 100) -> tuple[dict, ...]:
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(AdmissionOutboxRow)
                    .where(AdmissionOutboxRow.published_at.is_(None))
                    .order_by(AdmissionOutboxRow.occurred_at, AdmissionOutboxRow.event_id)
                    .limit(limit)
                )
            ).all()
        return tuple(json.loads(row.event_json) for row in rows)

    async def record_publish_failure(self, *, event_id: str, error: str) -> None:
        async with self.lock:
            async with self.database.sessions.begin() as session:
                row = await session.get(AdmissionOutboxRow, event_id)
                if row is None or row.published_at is not None:
                    return
                row.publish_attempts += 1
                row.last_error = error[:512]

    async def mark_published(self, *, event_id: str, published_at: datetime) -> None:
        async with self.lock:
            async with self.database.sessions.begin() as session:
                row = await session.get(AdmissionOutboxRow, event_id)
                if row is None:
                    raise KeyError(event_id)
                if row.published_at is None:
                    row.published_at = published_at
                    row.last_error = ""
