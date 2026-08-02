from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.acquire_device_channels import AcquireDeviceChannels
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    ChannelState,
    OpaqueChannelBinding,
)
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.domain.sessions.entities import DeviceSessionLease

NOW = datetime(2026, 8, 2, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self, device):
        self.device = device

    async def get(self, device_id):
        return self.device if self.device.identity.device_id == device_id else None


class _Sessions:
    def __init__(self, session):
        self.session = session

    async def get(self, session_id):
        return self.session if self.session.session_id == session_id else None


class _Channels:
    def __init__(self):
        self.values = {}

    async def upsert(self, lease):
        self.values[lease.channel_id] = lease
        return lease

    async def list_for_device(self, device_id):
        return tuple(item for item in self.values.values() if item.device_id == device_id)


class _Provider:
    def __init__(self):
        self.contexts = []

    async def acquire_channels(self, context):
        self.contexts.append(context)
        lease = ChannelLease(
            channel_id="channel-1",
            device_id=context.device_id,
            purpose="management",
            kinds=frozenset({ChannelKind.RELIABLE_DATA}),
            binding_format="application/vnd.eidolon.channel-binding+json",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        return ChannelAssignmentSet(
            operation_id=context.operation_id,
            device_id=context.device_id,
            manifest_revision=context.manifest_revision,
            grants=(
                ChannelGrant(
                    operation_id=context.operation_id,
                    lease=lease,
                    opaque_binding=OpaqueChannelBinding(b"provider-owned-binding"),
                ),
            ),
        )


def _use_case(*, provider=None, channels=None):
    return AcquireDeviceChannels(
        hub_id="hub-1",
        devices=_Devices(_device()),
        sessions=_Sessions(_session()),
        channels=channels or _Channels(),
        provider=provider or _Provider(),
        clock=_Clock(),
    )


def _device(*, approved=True, revoked=False):
    return ManagedDevice(
        identity=DeviceIdentity("device-1", "p256:fingerprint", "tenant-1"),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1, "title": "Device"}),
        registered_at=NOW,
        updated_at=NOW,
        owner_id="owner-1",
        approved=approved,
        revoked=revoked,
    )


def _session():
    return DeviceSessionLease(
        session_id="session-1",
        device_id="device-1",
        opened_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        lease_token="session-token-0000000000000001",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-1",
        fencing_token=1,
    )


async def test_acquire_relays_provider_binding_and_persists_only_channel_metadata() -> None:
    provider = _Provider()
    channels = _Channels()
    use_case = AcquireDeviceChannels(
        hub_id="hub-1",
        devices=_Devices(_device()),
        sessions=_Sessions(_session()),
        channels=channels,
        provider=provider,
        clock=_Clock(),
    )

    assignments = await use_case.execute(
        session_id="session-1",
        lease_token="session-token-0000000000000001",
        request_id="acquire-1",
    )

    assert assignments.grants[0].opaque_binding.relay_bytes() == b"provider-owned-binding"
    assert channels.values["channel-1"] == assignments.grants[0].lease
    assert provider.contexts[0].operation_id == "acquire-1"
    assert provider.contexts[0].manifest_json == _device().manifest_json


@pytest.mark.parametrize(
    ("device", "token"),
    [
        (_device(approved=False), "session-token-0000000000000001"),
        (_device(revoked=True), "session-token-0000000000000001"),
        (_device(), "wrong-session-token-000000001"),
    ],
)
async def test_acquire_rejects_unavailable_or_unauthenticated_device(device, token) -> None:
    provider = _Provider()
    use_case = AcquireDeviceChannels(
        hub_id="hub-1",
        devices=_Devices(device),
        sessions=_Sessions(_session()),
        channels=_Channels(),
        provider=provider,
        clock=_Clock(),
    )

    with pytest.raises(PermissionError):
        await use_case.execute(
            session_id="session-1",
            lease_token=token,
            request_id="acquire-1",
        )

    assert provider.contexts == []


@pytest.mark.parametrize("mutation", ("identity", "missing-management", "expired", "active"))
async def test_invalid_provider_assignments_are_not_persisted(mutation) -> None:
    class _InvalidProvider(_Provider):
        async def acquire_channels(self, context):
            response = await super().acquire_channels(context)
            grant = response.grants[0]
            if mutation == "identity":
                return replace(response, operation_id="another-request")
            if mutation == "missing-management":
                grant = replace(grant, lease=replace(grant.lease, purpose="telemetry"))
            elif mutation == "expired":
                grant = replace(
                    grant,
                    lease=replace(
                        grant.lease,
                        issued_at=NOW - timedelta(minutes=1),
                        expires_at=NOW + timedelta(seconds=20),
                    ),
                )
            elif mutation == "active":
                grant = replace(grant, lease=replace(grant.lease, state=ChannelState.ACTIVE))
            return replace(response, grants=(grant,))

    channels = _Channels()
    with pytest.raises(ValueError):
        await _use_case(provider=_InvalidProvider(), channels=channels).execute(
            session_id="session-1",
            lease_token="session-token-0000000000000001",
            request_id="acquire-1",
        )

    assert channels.values == {}


async def test_provider_outage_is_visible_and_does_not_change_channel_metadata() -> None:
    class _UnavailableProvider:
        async def acquire_channels(self, context):
            raise ConnectionError("provider unavailable")

    channels = _Channels()
    previous = ChannelLease(
        channel_id="previous-channel",
        device_id="device-1",
        purpose="management",
        kinds=frozenset({ChannelKind.RELIABLE_DATA}),
        binding_format="application/previous+json",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
        state=ChannelState.ACTIVE,
    )
    await channels.upsert(previous)

    with pytest.raises(ConnectionError, match="provider unavailable"):
        await _use_case(provider=_UnavailableProvider(), channels=channels).execute(
            session_id="session-1",
            lease_token="session-token-0000000000000001",
            request_id="acquire-1",
        )

    assert channels.values == {"previous-channel": previous}


async def test_reacquire_preserves_active_assignment_and_closes_missing_metadata() -> None:
    channels = _Channels()
    active = ChannelLease(
        channel_id="channel-1",
        device_id="device-1",
        purpose="management",
        kinds=frozenset({ChannelKind.RELIABLE_DATA}),
        binding_format="application/old+json",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
        state=ChannelState.ACTIVE,
        updated_at=NOW,
    )
    stale = replace(active, channel_id="stale-channel")
    await channels.upsert(active)
    await channels.upsert(stale)

    await _use_case(channels=channels).execute(
        session_id="session-1",
        lease_token="session-token-0000000000000001",
        request_id="acquire-1",
    )

    assert channels.values["channel-1"].state is ChannelState.ACTIVE
    assert channels.values["stale-channel"].state is ChannelState.CLOSED
