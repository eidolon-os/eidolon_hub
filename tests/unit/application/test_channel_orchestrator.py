from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.provision_channel import ProvisionChannel
from hub.application.use_cases.renew_channel import RenewChannel
from hub.application.use_cases.revoke_channel import RevokeChannel
from hub.domain.channels.entities import (
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    ChannelProfile,
    OpaqueChannelBinding,
)
from hub.domain.channels.selection import ChannelProfileCatalog
from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument


class _Clock:
    value = datetime(2026, 8, 1, tzinfo=UTC)

    def now(self):
        return self.value


class _Ids:
    def new(self, prefix: str) -> str:
        return f"{prefix}-1"


class _Devices:
    def __init__(self, device):
        self.device = device

    async def get(self, device_id: str):
        return self.device if self.device.identity.device_id == device_id else None


class _Connections:
    def __init__(self, lease):
        self.lease = lease

    async def active_for_device(self, device_id: str, *, now: datetime):
        return (self.lease,) if self.lease.device_id == device_id else ()


class _Provisioner:
    def __init__(self, now: datetime, binding: bytes):
        self.now = now
        self.binding = binding
        self.requests = []
        self.revoked = []

    async def provision(self, request):
        self.requests.append(request)
        return ChannelGrant(
            request_id=request.request_id,
            lease=ChannelLease(
                channel_id="provider-channel-1",
                device_id=request.device_id,
                profile_name=request.profile.name,
                issued_at=self.now,
                expires_at=self.now + timedelta(minutes=5),
            ),
            opaque_binding=OpaqueChannelBinding(self.binding),
        )

    async def renew(self, lease):
        return ChannelGrant(
            request_id="renew-request-1",
            lease=ChannelLease(
                channel_id=lease.channel_id,
                device_id=lease.device_id,
                profile_name=lease.profile_name,
                issued_at=self.now,
                expires_at=self.now + timedelta(minutes=10),
            ),
            opaque_binding=OpaqueChannelBinding(self.binding),
        )

    async def revoke(self, grant, *, reason: str):
        self.revoked.append((grant, reason))


class _GrantSender:
    def __init__(self):
        self.sent = []

    async def send_grant(self, *, signaling_ref: str, grant):
        self.sent.append((signaling_ref, grant))


class _FailingGrantSender:
    async def send_grant(self, *, signaling_ref: str, grant):
        raise ConnectionError("signaling unavailable")


class _ChannelLeases:
    def __init__(self):
        self.values = {}

    async def get(self, channel_id):
        return self.values.get(channel_id)

    async def upsert(self, lease):
        self.values[lease.channel_id] = lease
        return lease

    async def delete(self, channel_id):
        self.values.pop(channel_id, None)


def _device(now: datetime) -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity("device-1", "p256:fingerprint"),
        display_name="Camera",
        device_kind="camera",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        registered_at=now,
        updated_at=now,
        approved=True,
    )


def _connection(now: datetime) -> ConnectionLease:
    return ConnectionLease(
        connection_id="mqtt-1",
        device_id="device-1",
        connector_id="mqtt-cloud",
        connector_kind=ConnectorKind.MQTT5,
        signaling_ref="mqtt:device-1",
        opened_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=45),
        lease_token="lease-token-device-1",
        identity_fingerprint="p256:fingerprint",
        hub_instance_id="hub-cloud",
        fencing_token=1,
    )


@pytest.mark.asyncio
async def test_provider_binding_is_relayed_unchanged_and_redacted() -> None:
    now = _Clock.value
    raw_binding = b'{"url":"wss://provider","room":"secret","token":"secret"}'
    provisioner = _Provisioner(now, raw_binding)
    sender = _GrantSender()
    profile = ChannelProfile(
        name="realtime-media",
        required_kinds=frozenset({ChannelKind.REALTIME_DATA, ChannelKind.AUDIO, ChannelKind.VIDEO}),
        provisioner_ref="channel-provisioner/realtime",
    )
    use_case = ProvisionChannel(
        profiles=ChannelProfileCatalog((profile,)),
        provisioners={profile.provisioner_ref: provisioner},
        grant_sender=sender,
        devices=_Devices(_device(now)),
        connections=_Connections(_connection(now)),
        channel_leases=_ChannelLeases(),
        clock=_Clock(),
        ids=_Ids(),
    )

    grant = await use_case.execute(device_id="device-1", profile_name="realtime-media")

    assert grant.opaque_binding.relay_bytes() == raw_binding
    assert sender.sent == [("mqtt:device-1", grant)]
    assert "wss://provider" not in repr(grant)
    assert "secret" not in repr(grant)


@pytest.mark.asyncio
async def test_channel_renewal_and_revoke_use_lease_metadata_not_old_binding() -> None:
    now = _Clock.value
    profile = ChannelProfile(
        "management-data",
        frozenset({ChannelKind.RELIABLE_DATA}),
        "channel-provisioner/default-data",
    )
    provider = _Provisioner(now, b"new-encrypted-binding")
    leases = _ChannelLeases()
    original = ChannelLease(
        channel_id="provider-channel-1",
        device_id="device-1",
        profile_name=profile.name,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    await leases.upsert(original)
    sender = _GrantSender()
    providers = {profile.provisioner_ref: provider}
    renewed = await RenewChannel(
        profiles=ChannelProfileCatalog((profile,)),
        provisioners=providers,
        leases=leases,
        connections=_Connections(_connection(now)),
        grant_sender=sender,
        clock=_Clock(),
    ).execute(original.channel_id)

    assert renewed.opaque_binding.relay_bytes() == b"new-encrypted-binding"
    assert (await leases.get(original.channel_id)).expires_at == now + timedelta(minutes=10)
    await RevokeChannel(
        profiles=ChannelProfileCatalog((profile,)),
        provisioners=providers,
        leases=leases,
    ).execute(original.channel_id, reason="test")
    assert await leases.get(original.channel_id) is None
    assert provider.revoked[-1] == (renewed.lease, "test")


@pytest.mark.asyncio
async def test_failed_renew_grant_delivery_revokes_new_provider_lease() -> None:
    now = _Clock.value
    profile = ChannelProfile(
        "management-data",
        frozenset({ChannelKind.RELIABLE_DATA}),
        "channel-provisioner/default-data",
    )
    provider = _Provisioner(now, b"new-encrypted-binding")
    leases = _ChannelLeases()
    original = ChannelLease(
        channel_id="provider-channel-1",
        device_id="device-1",
        profile_name=profile.name,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    await leases.upsert(original)

    with pytest.raises(ConnectionError, match="signaling unavailable"):
        await RenewChannel(
            profiles=ChannelProfileCatalog((profile,)),
            provisioners={profile.provisioner_ref: provider},
            leases=leases,
            connections=_Connections(_connection(now)),
            grant_sender=_FailingGrantSender(),
            clock=_Clock(),
        ).execute(original.channel_id)

    assert await leases.get(original.channel_id) is None
    assert provider.revoked[-1][1] == "renew-grant-delivery-failed"


def test_smallest_matching_profile_is_selected_without_provider_checks() -> None:
    data = ChannelProfile(
        "management-data",
        frozenset({ChannelKind.RELIABLE_DATA}),
        "channel-provisioner/default-data",
    )
    media = ChannelProfile(
        "realtime-media",
        frozenset(
            {
                ChannelKind.RELIABLE_DATA,
                ChannelKind.REALTIME_DATA,
                ChannelKind.AUDIO,
                ChannelKind.VIDEO,
            }
        ),
        "channel-provisioner/realtime",
    )

    selected = ChannelProfileCatalog((media, data)).select(frozenset({ChannelKind.RELIABLE_DATA}))

    assert selected.name == "management-data"
