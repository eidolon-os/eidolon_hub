"""Transactional persistence and outbox primitives for canonical Admission."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    AdmissionClaimEventStreamRow,
    AdmissionClaimRow,
    AdmissionCommandResultRow,
    AdmissionDecisionRow,
    AdmissionGrantAckRow,
    AdmissionGrantRow,
    AdmissionOutboxRow,
    AdmissionProposalRow,
    DeviceRow,
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

    async def get_decision_for_enrollment(self, session, enrollment_id: str):
        return await session.scalar(
            select(AdmissionDecisionRow).where(AdmissionDecisionRow.enrollment_id == enrollment_id)
        )

    async def get_ack_for_enrollment(self, session, enrollment_id: str):
        return await session.scalar(
            select(AdmissionGrantAckRow).where(AdmissionGrantAckRow.enrollment_id == enrollment_id)
        )

    async def list_proposals(
        self,
        session,
        *,
        owner_domain_id: str,
        states: tuple[str, ...],
        cursor: tuple[datetime, str] | None,
        limit: int,
    ):
        statement = select(AdmissionProposalRow).where(
            AdmissionProposalRow.requested_owner_domain_id == owner_domain_id,
            AdmissionProposalRow.state.in_(states),
        )
        if cursor is not None:
            sort_key, resource_id = cursor
            statement = statement.where(
                or_(
                    AdmissionProposalRow.created_at > sort_key,
                    and_(
                        AdmissionProposalRow.created_at == sort_key,
                        AdmissionProposalRow.enrollment_id > resource_id,
                    ),
                )
            )
        return (
            await session.scalars(
                statement.order_by(
                    AdmissionProposalRow.created_at, AdmissionProposalRow.enrollment_id
                ).limit(limit)
            )
        ).all()

    async def list_claims(
        self,
        session,
        *,
        owner_domain_id: str,
        business_owner_id: str,
        states: tuple[str, ...],
        cursor: tuple[datetime, str] | None,
        limit: int,
    ):
        statement = select(AdmissionClaimRow).where(
            AdmissionClaimRow.owner_domain_id == owner_domain_id,
            AdmissionClaimRow.business_owner_id == business_owner_id,
            AdmissionClaimRow.state.in_(states),
        )
        if cursor is not None:
            sort_key, resource_id = cursor
            statement = statement.where(
                or_(
                    AdmissionClaimRow.updated_at > sort_key,
                    and_(
                        AdmissionClaimRow.updated_at == sort_key,
                        AdmissionClaimRow.device_instance_id > resource_id,
                    ),
                )
            )
        return (
            await session.scalars(
                statement.order_by(
                    AdmissionClaimRow.updated_at, AdmissionClaimRow.device_instance_id
                ).limit(limit)
            )
        ).all()

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

    @staticmethod
    async def project_active_claim(
        session,
        *,
        device_ref,
        business_owner_id: str,
        manifest_id: str,
        manifest_json: str,
        manifest_digest: str,
        activated_at: datetime,
    ) -> None:
        """Update the owner-facing read model in the Claim ACK transaction.

        This table is not a second lifecycle authority.  It contains only
        query and naming data derived after the canonical Claim becomes active;
        notably there is no enrollment id or retrieval capability to dual-write.
        """

        document = json.loads(manifest_json)
        if not isinstance(document, dict):
            raise AdmissionProblem("INVALID_ARGUMENT", "Manifest document must be an object")
        title = document.get("title")
        display_name = title.strip() if isinstance(title, str) and title.strip() else device_ref.device_instance_id
        row = await session.get(DeviceRow, device_ref.device_instance_id)
        values = {
            "display_name": row.display_name if row is not None else display_name,
            "device_kind": manifest_id,
            "manifest_json": manifest_json,
            "manifest_revision": manifest_digest,
            "enrolled_at": activated_at,
            "updated_at": activated_at,
            "owner_domain_generation": device_ref.owner_domain_generation,
            "claim_generation": device_ref.claim_generation,
            "trust_epoch": device_ref.trust_epoch,
            "aggregate_revision": 1 if row is None else row.aggregate_revision + 1,
            "owner_id": business_owner_id,
            "lifecycle_state": "approved",
            "last_management_request_id": "",
            "last_management_fingerprint": "",
        }
        if row is None:
            session.add(DeviceRow(device_id=device_ref.device_instance_id, **values))
            return
        for name, value in values.items():
            setattr(row, name, value)

    @staticmethod
    async def project_revoked_claim(session, *, device_ref, revoked_at: datetime) -> None:
        """Mark only the matching Claim generation's directory row revoked."""

        row = await session.get(DeviceRow, device_ref.device_instance_id)
        if row is None:
            return
        if (
            row.owner_domain_generation,
            row.claim_generation,
            row.trust_epoch,
        ) != (
            device_ref.owner_domain_generation,
            device_ref.claim_generation,
            device_ref.trust_epoch,
        ):
            return
        row.lifecycle_state = "revoked"
        row.updated_at = revoked_at
        row.aggregate_revision += 1

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
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
        session.add(
            AdmissionOutboxRow(
                event_id=event_id,
                event_type=event_type,
                source="urn:eidolon:authority:admission",
                aggregate_id=aggregate_id,
                aggregate_revision=aggregate_revision,
                event_json=encoded,
                occurred_at=occurred_at,
                published_at=None,
                publish_attempts=0,
                last_error="",
            )
        )
        if event_type in {
            "live.eidolon.device.claim-activated.v1",
            "live.eidolon.device.claim-revoked.v1",
        }:
            session.add(
                AdmissionClaimEventStreamRow(
                    event_id=event_id,
                    owner_domain_id=event["ownerdomainid"],
                    event_type=event_type,
                    event_json=encoded,
                    occurred_at=occurred_at,
                )
            )

    async def claim_event_page(
        self, *, after: int, limit: int
    ) -> tuple[tuple[tuple[int, dict], ...], int]:
        async with self.database.sessions() as session:
            high_watermark = (
                await session.scalar(select(func.max(AdmissionClaimEventStreamRow.stream_position)))
                or 0
            )
            if after > high_watermark:
                raise AdmissionProblem("CURSOR_GAP", "Claim event cursor is ahead of the stream")
            if after and await session.get(AdmissionClaimEventStreamRow, after) is None:
                raise AdmissionProblem("CURSOR_GAP", "Claim event cursor is no longer retained")
            rows = (
                await session.scalars(
                    select(AdmissionClaimEventStreamRow)
                    .where(AdmissionClaimEventStreamRow.stream_position > after)
                    .order_by(AdmissionClaimEventStreamRow.stream_position)
                    .limit(limit)
                )
            ).all()
        positions = [row.stream_position for row in rows]
        if positions and positions != list(range(after + 1, after + 1 + len(positions))):
            raise AdmissionProblem("CURSOR_GAP", "Claim event stream contains a durable gap")
        return tuple(
            (row.stream_position, json.loads(row.event_json)) for row in rows
        ), high_watermark

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
