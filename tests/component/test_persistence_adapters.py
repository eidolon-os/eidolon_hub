from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.repositories import AuthorityLeaseHeld, SqlHubRepositories
from hub.domain.channels.entities import (
    ChannelKind,
    ChannelLease,
    ChannelState,
    ProviderSyncRecord,
    ProviderSyncState,
)
from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.domain.devices.entities import DeviceDirectoryEntry, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.event_bus import DomainEvent
from hub.ports.identity import EnrollmentChallenge

NOW = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path):
    value = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await value.init_schema()
    try:
        yield value
    finally:
        await value.close()


def _device() -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity("device-1", "p256:fingerprint", "tenant-1"),
        display_name="Generic Sensor",
        device_kind="environment-sensor",
        manifest=DeviceManifestDocument.from_mapping(
            {
                "actions": [],
                "events": [],
                "media": [],
                "properties": [],
                "schema_version": 1,
                "title": "Generic Sensor",
            }
        ),
        registered_at=NOW,
        updated_at=NOW,
    )


def _connection(*, fencing_token: int = 1) -> ConnectionLease:
    return ConnectionLease(
        connection_id="connection-1",
        device_id="device-1",
        connector_id="https-local",
        connector_kind=ConnectorKind.HTTPS,
        signaling_ref="http-mailbox:device-1",
        opened_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=45),
        lease_token="private-token",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=fencing_token,
    )


async def test_device_and_command_round_trip_are_idempotent(database) -> None:
    repositories = SqlHubRepositories(database)
    device = _device()
    command = DeviceCommand(
        command_id="command-1",
        device_id="device-1",
        operation="sensor.calibrate",
        payload_json='{"offset":1}',
        state=CommandState.QUEUED,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        updated_at=NOW,
    )

    await repositories.devices.upsert(device)
    await repositories.devices.upsert(device)
    await repositories.commands.upsert(command)

    assert await repositories.devices.get("device-1") == device
    assert await repositories.devices.list_all() == (device,)
    assert await repositories.commands.get("command-1") == command


async def test_connection_repository_enforces_fencing_and_queries_active(database) -> None:
    repository = SqlHubRepositories(database).connections
    lease = _connection(fencing_token=2)
    await repository.upsert(lease)

    assert await repository.active_for_device("device-1", now=NOW) == (lease,)
    with pytest.raises(PermissionError, match="stale"):
        await repository.upsert(_connection(fencing_token=1))


async def test_authority_lease_fences_takeover_after_expiry(database) -> None:
    repository = SqlHubRepositories(database).authority
    first = await repository.acquire(
        device_id="device-1",
        hub_instance_id="hub-1",
        now=NOW,
        ttl=timedelta(seconds=30),
    )
    with pytest.raises(AuthorityLeaseHeld):
        await repository.acquire(
            device_id="device-1",
            hub_instance_id="hub-2",
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    second = await repository.acquire(
        device_id="device-1",
        hub_instance_id="hub-2",
        now=NOW + timedelta(seconds=31),
        ttl=timedelta(seconds=30),
    )

    assert first.fencing_token == 1
    assert second.fencing_token == 2


async def test_challenge_consumption_is_durable_and_single_use(database) -> None:
    repository = SqlHubRepositories(database).challenges
    challenge = EnrollmentChallenge(
        challenge_id="challenge-1",
        device_id="device-1",
        client_nonce="client",
        server_nonce="server",
        expires_at=NOW + timedelta(seconds=30),
        connector_id="https-local",
        connector_kind="https",
        signaling_ref="http-mailbox:device-1",
        priority=100,
    )
    await repository.create(challenge)

    assert (await repository.consume("challenge-1")).consumed
    with pytest.raises(PermissionError, match="already consumed"):
        await repository.consume("challenge-1")


async def test_directory_channel_and_cursor_runtime_state_survives_reopen(database) -> None:
    repositories = SqlHubRepositories(database)
    directory = DeviceDirectoryEntry(
        device_id="device-1",
        owner_scope="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json="{}",
        manifest_revision="sha256:manifest",
        approved=True,
        revoked=False,
        online=False,
        connections=(),
        registered_at=NOW,
        updated_at=NOW,
    )
    channel = ChannelLease(
        channel_id="channel-1",
        device_id="device-1",
        purpose="management",
        kinds=frozenset({ChannelKind.RELIABLE_DATA}),
        binding_format="application/eidolon-test+json",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        state=ChannelState.ACTIVE,
    )

    first = await repositories.directory.upsert(directory)
    second = await repositories.directory.upsert(
        replace(directory, online=True, updated_at=NOW + timedelta(seconds=1))
    )
    await repositories.channel_leases.upsert(channel)
    assert await repositories.channel_cursors.next_outbound("channel-1") == 1
    assert await repositories.channel_cursors.next_outbound("channel-1") == 2
    assert await repositories.channel_cursors.accept_inbound(
        channel_id="channel-1", sequence=1, envelope_id="envelope-1"
    )
    assert not await repositories.channel_cursors.accept_inbound(
        channel_id="channel-1", sequence=1, envelope_id="envelope-1"
    )

    assert first.revision == 1 and second.revision == 2
    assert await repositories.directory.list(owner_scope="owner-1") == (second,)
    assert await repositories.channel_leases.active_for_device(
        "device-1", now=NOW, purpose="management"
    ) == (channel,)


async def test_channel_provider_sync_claim_is_durable_and_fenced(database) -> None:
    repository = SqlHubRepositories(database).channel_provider_sync
    desired = ProviderSyncRecord(
        device_id="device-1",
        operation_id="channel-sync:sha256:desired",
        desired_revision="sha256:desired",
        state=ProviderSyncState.PENDING,
        attempts=0,
        updated_at=NOW,
    )

    first = await repository.try_claim(
        desired,
        owner_instance_id="hub-1",
        now=NOW,
        claim_ttl=timedelta(seconds=30),
    )
    competing = await repository.try_claim(
        desired,
        owner_instance_id="hub-2",
        now=NOW + timedelta(seconds=1),
        claim_ttl=timedelta(seconds=30),
    )
    takeover = await repository.try_claim(
        desired,
        owner_instance_id="hub-2",
        now=NOW + timedelta(seconds=31),
        claim_ttl=timedelta(seconds=30),
    )
    await repository.mark_succeeded(
        device_id="device-1",
        operation_id=desired.operation_id,
        now=NOW + timedelta(seconds=32),
    )
    replay = await repository.try_claim(
        desired,
        owner_instance_id="hub-2",
        now=NOW + timedelta(seconds=33),
        claim_ttl=timedelta(seconds=30),
    )

    assert first is not None and first.attempts == 1
    assert competing is None
    assert takeover is not None and takeover.attempts == 2
    assert replay is not None and replay.attempts == 3
    assert replay.state is ProviderSyncState.SYNCHRONIZING


async def test_domain_event_stream_is_owner_scoped_and_idempotent(database) -> None:
    repositories = SqlHubRepositories(database)
    await repositories.devices.upsert(replace(_device(), owner_id="owner-1"))
    domain_event = DomainEvent(
        event_id="event-domain",
        event_type="eidolon.device.registered",
        source="/eidolon-hub",
        subject="device-1",
        occurred_at=NOW,
        data_json='{"ok":true}',
    )
    transferred_event = replace(
        domain_event,
        event_id="event-after-transfer",
        event_type="eidolon.device.transferred",
        data_json='{"owner":"owner-2"}',
    )
    await repositories.events.publish(domain_event)
    await repositories.events.publish(domain_event)
    await repositories.devices.upsert(replace(_device(), owner_id="owner-2"))
    await repositories.events.publish(transferred_event)

    stream = await repositories.events.list_after(
        owner_scope="owner-1", stream_position=0, limit=100
    )
    assert [item.event for item in stream] == [domain_event]
    assert stream[0].stream_position >= 1
    assert [
        item.event
        for item in await repositories.events.list_after(
            owner_scope="owner-2", stream_position=0, limit=100
        )
    ] == [transferred_event]
