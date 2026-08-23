from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    ClaimCommandResultRow,
    ClaimEventRow,
    DeviceManagementEventRow,
    DeviceRow,
)
from hub.adapters.persistence.repositories import SqlDeviceRepository, SqlHubRepositories
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Ids:
    def __init__(self, suffix: str) -> None:
        self._suffix = suffix

    def new(self, prefix: str) -> str:
        return f"{prefix}-{self._suffix}"


class _Projector:
    async def execute(self, device_id: str) -> None:
        del device_id


def _device(device_id: str, *, generation: int = 1) -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity(device_id),
        enrollment_id=f"enrollment-{device_id}-{generation}",
        retrieval_token_hash="a" * 64,
        retrieval_expires_at=NOW + timedelta(minutes=10),
        display_name=device_id,
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        enrolled_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(minutes=1),
        claim_generation=generation,
        trust_epoch=1,
        aggregate_revision=generation,
        owner_id="owner-1",
        lifecycle_state=DeviceLifecycleState.APPROVED,
    )


async def _seed(database: HubDatabase, *devices: ManagedDevice) -> None:
    async with database.sessions.begin() as session:
        for device in devices:
            session.add(DeviceRow(**SqlDeviceRepository._values(device)))


def _revoke(repositories: SqlHubRepositories, *, event_suffix: str) -> RevokeDevice:
    return RevokeDevice(
        devices=repositories.devices,
        claims=repositories.claim_lifecycle,
        clock=_Clock(),
        ids=_Ids(event_suffix),
        directory_projector=_Projector(),
    )


async def test_claim_command_event_cursor_and_projection_survive_restart(tmp_path) -> None:
    path = tmp_path / "hub.sqlite3"
    first_database = HubDatabase.sqlite(path)
    await first_database.initialize_schema()
    first_repositories = SqlHubRepositories(first_database)
    first_device = _device("device-1")
    second_device = _device("device-2")
    await _seed(first_database, first_device, second_device)

    first = await _revoke(first_repositories, event_suffix="one").execute(
        device_ref=first_device.device_ref,
        reason="owner-removed",
        command_id="revoke-command-1",
        correlation_id="removal-intent-1",
        principal_id="controller-1",
    )
    await first_database.close()

    restarted_database = HubDatabase.sqlite(path)
    await restarted_database.initialize_schema()
    restarted_repositories = SqlHubRepositories(restarted_database)
    replay = await _revoke(restarted_repositories, event_suffix="unused").execute(
        device_ref=first_device.device_ref,
        reason="owner-removed",
        command_id="revoke-command-1",
        correlation_id="another-audit-chain",
        principal_id="controller-2",
    )
    second = await _revoke(restarted_repositories, event_suffix="two").execute(
        device_ref=second_device.device_ref,
        reason="owner-removed",
        command_id="revoke-command-2",
        correlation_id="removal-intent-2",
        principal_id="controller-2",
    )

    first_page = await restarted_repositories.claim_lifecycle.list_events_after(
        stream_position=0, limit=1
    )
    second_page = await restarted_repositories.claim_lifecycle.list_events_after(
        stream_position=first_page[-1].stream_position, limit=1
    )
    assert replay.outcome == "replayed"
    assert replay.event_id == first.event_id
    assert replay.occurred_at == first.occurred_at
    assert [item.event.event_id for item in first_page + second_page] == [
        first.event_id,
        second.event_id,
    ]
    assert second_page[0].stream_position > first_page[0].stream_position
    assert await restarted_repositories.device_control.materialize_claim_events(now=NOW) == 2
    assert await restarted_repositories.device_control.materialize_claim_events(now=NOW) == 0
    await restarted_database.close()

    relocated_database = HubDatabase.sqlite(path)
    await relocated_database.initialize_schema()
    relocated_repositories = SqlHubRepositories(relocated_database)
    try:
        replayed_stream = await relocated_repositories.claim_lifecycle.list_events_after(
            stream_position=0, limit=500
        )
        assert [item.event.event_id for item in replayed_stream] == [
            first.event_id,
            second.event_id,
        ]
        assert await relocated_repositories.device_control.materialize_claim_events(now=NOW) == 0
        assert (
            await relocated_repositories.device_control.get_by_event_id(
                event_id=first.event_id
            )
        ) is not None
    finally:
        await relocated_database.close()


async def test_claim_transaction_rolls_back_state_result_and_audit_on_event_conflict(
    tmp_path,
) -> None:
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await database.initialize_schema()
    repositories = SqlHubRepositories(database)
    device = _device("device-1")
    await _seed(database, device)
    async with database.sessions.begin() as session:
        session.add(
            ClaimEventRow(
                event_id="claim-event-collision",
                event_type="live.eidolon.device.claim-revoked.v1",
                device_id="other-device",
                owner_domain_id="owner-1",
                claim_generation=1,
                trust_epoch=1,
                accepted_manifest_digest=device.manifest_revision,
                aggregate_revision=2,
                correlation_id="other-intent",
                causation_id="other-command",
                actor_principal_id="controller-2",
                occurred_at=NOW,
                reason="other-removal",
            )
        )

    with pytest.raises(IntegrityError):
        await _revoke(repositories, event_suffix="collision").execute(
            device_ref=device.device_ref,
            reason="owner-removed",
            command_id="revoke-command-1",
            correlation_id="removal-intent-1",
            principal_id="controller-1",
        )

    persisted = await repositories.devices.get("device-1")
    async with database.sessions() as session:
        command_count = await session.scalar(select(func.count()).select_from(ClaimCommandResultRow))
        audit_count = await session.scalar(select(func.count()).select_from(DeviceManagementEventRow))
        event_count = await session.scalar(select(func.count()).select_from(ClaimEventRow))
    try:
        assert persisted is not None
        assert persisted.lifecycle_state is DeviceLifecycleState.APPROVED
        assert command_count == 0
        assert audit_count == 0
        assert event_count == 1
    finally:
        await database.close()


async def test_old_generation_is_fenced_and_new_generation_gets_a_new_event(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await database.initialize_schema()
    repositories = SqlHubRepositories(database)
    first_generation = _device("device-1", generation=1)
    await _seed(database, first_generation)
    first = await _revoke(repositories, event_suffix="generation-1").execute(
        device_ref=first_generation.device_ref,
        reason="owner-removed",
        command_id="revoke-command-1",
        correlation_id="removal-intent-1",
        principal_id="controller-1",
    )

    async with database.sessions.begin() as session:
        row = await session.get(DeviceRow, "device-1")
        assert row is not None
        row.enrollment_id = "enrollment-device-1-2"
        row.claim_generation = 2
        row.aggregate_revision = 3
        row.lifecycle_state = DeviceLifecycleState.APPROVED.value
        row.updated_at = NOW + timedelta(seconds=1)
    second_generation = await repositories.devices.get("device-1")
    assert second_generation is not None

    use_case = _revoke(repositories, event_suffix="generation-2")
    with pytest.raises(LookupError, match="stale"):
        await use_case.execute(
            device_ref=first_generation.device_ref,
            reason="owner-removed",
            command_id="stale-generation-command",
            correlation_id="removal-intent-stale",
            principal_id="controller-1",
        )
    with pytest.raises(ValueError, match="different content"):
        await use_case.execute(
            device_ref=second_generation.device_ref,
            reason="owner-removed",
            command_id="revoke-command-1",
            correlation_id="removal-intent-2",
            principal_id="controller-1",
        )
    second = await use_case.execute(
        device_ref=second_generation.device_ref,
        reason="owner-removed",
        command_id="revoke-command-2",
        correlation_id="removal-intent-2",
        principal_id="controller-1",
    )
    try:
        stream = await repositories.claim_lifecycle.list_events_after(
            stream_position=0, limit=500
        )
        assert [item.event.device_ref.claim_generation for item in stream] == [1, 2]
        assert first.event_id != second.event_id
    finally:
        await database.close()
