from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hub.application.use_cases.reconcile_device_channels import ReconcileDeviceChannels
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    ChannelState,
    OpaqueChannelBinding,
    ProviderSyncState,
)
from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    value = NOW

    def now(self):
        return self.value


class _Devices:
    def __init__(self, device):
        self.device = device

    async def list_all(self):
        return (self.device,)


class _Connections:
    def __init__(self, active=True):
        self.active = active
        self.lease = ConnectionLease(
            connection_id="connection-1",
            device_id="device-1",
            connector_id="https-local",
            connector_kind=ConnectorKind.HTTPS,
            signaling_ref="http-mailbox:device-1",
            opened_at=NOW,
            renewed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            lease_token="lease-token-device-1",
            identity_fingerprint="p256:fingerprint",
            hub_instance_id="hub-local-1",
            fencing_token=1,
        )

    async def active_for_device(self, device_id, *, now):
        return (self.lease,) if self.active and device_id == "device-1" else ()


class _Syncs:
    def __init__(self):
        self.value = None

    async def get(self, device_id):
        return self.value if self.value and self.value.device_id == device_id else None

    async def try_claim(self, desired, *, owner_instance_id, now, claim_ttl):
        attempts = (
            self.value.attempts + 1
            if self.value and self.value.desired_revision == desired.desired_revision
            else 1
        )
        self.value = replace(
            desired,
            state=ProviderSyncState.SYNCHRONIZING,
            owner_instance_id=owner_instance_id,
            claim_expires_at=now + claim_ttl,
            attempts=attempts,
        )
        return self.value

    async def mark_succeeded(self, *, device_id, operation_id, now):
        self.value = replace(
            self.value,
            state=ProviderSyncState.SUCCEEDED,
            owner_instance_id="",
            claim_expires_at=None,
            updated_at=now,
            last_error="",
        )

    async def mark_failed(self, *, device_id, operation_id, now, error):
        self.value = replace(
            self.value,
            state=ProviderSyncState.FAILED,
            owner_instance_id="",
            claim_expires_at=None,
            updated_at=now,
            last_error=error,
        )


class _Leases:
    def __init__(self):
        self.values = {}

    async def upsert(self, lease):
        self.values[lease.channel_id] = lease
        return lease

    async def delete(self, channel_id):
        self.values.pop(channel_id, None)

    async def list_for_device(self, device_id):
        return tuple(value for value in self.values.values() if value.device_id == device_id)


class _Provider:
    def __init__(self, *, fail=False):
        self.contexts = []
        self.fail = fail

    async def sync_device(self, context):
        self.contexts.append(context)
        if self.fail:
            raise ConnectionError("provider unavailable")
        grants = ()
        if context.approved and context.connected and not context.revoked:
            grants = (
                ChannelGrant(
                    operation_id=context.operation_id,
                    lease=ChannelLease(
                        channel_id="channel-1",
                        device_id=context.device_id,
                        purpose="management",
                        kinds=frozenset({ChannelKind.RELIABLE_DATA}),
                        binding_format="application/eidolon-test+json",
                        issued_at=NOW,
                        expires_at=NOW + timedelta(minutes=5),
                        state=ChannelState.PENDING,
                    ),
                    opaque_binding=OpaqueChannelBinding(b"provider-secret-binding"),
                ),
            )
        return ChannelAssignmentSet(
            operation_id=context.operation_id,
            device_id=context.device_id,
            manifest_revision=context.manifest_revision,
            grants=grants,
        )


class _GrantSender:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send_grant(self, *, signaling_ref, grant):
        if self.error:
            raise self.error
        self.sent.append((signaling_ref, grant))


def _device(*, approved=True, revoked=False, owner_id=None):
    return ManagedDevice(
        identity=DeviceIdentity("device-1", "p256:fingerprint"),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
        registered_at=NOW,
        updated_at=NOW,
        owner_id=(owner_id if owner_id is not None else "owner-1") if approved else None,
        approved=approved,
        revoked=revoked,
    )


def _reconciler(device, *, connections=None, provider=None, syncs=None, leases=None, sender=None):
    return ReconcileDeviceChannels(
        hub_id="hub-local",
        hub_instance_id="hub-local-1",
        devices=_Devices(device),
        connections=connections or _Connections(),
        syncs=syncs or _Syncs(),
        channel_leases=leases or _Leases(),
        provider=provider or _Provider(),
        grant_sender=sender or _GrantSender(),
        clock=_Clock(),
    )


async def test_unapproved_device_never_calls_provider() -> None:
    provider = _Provider()

    result = await _reconciler(_device(approved=False), provider=provider).execute()

    assert result.skipped == 1
    assert provider.contexts == []


async def test_approved_connected_device_gets_pending_opaque_grant() -> None:
    provider, syncs, leases, sender = _Provider(), _Syncs(), _Leases(), _GrantSender()

    result = await _reconciler(
        _device(), provider=provider, syncs=syncs, leases=leases, sender=sender
    ).execute()

    assert result.succeeded == 1
    assert syncs.value.state is ProviderSyncState.SUCCEEDED
    assert leases.values["channel-1"].state is ChannelState.PENDING
    assert sender.sent[0][1].opaque_binding.relay_bytes() == b"provider-secret-binding"
    assert "provider-secret-binding" not in repr(sender.sent[0][1])


async def test_provider_failure_is_recorded_for_retry_not_raised() -> None:
    provider, syncs = _Provider(fail=True), _Syncs()

    result = await _reconciler(_device(), provider=provider, syncs=syncs).execute()

    assert result.failed == 1
    assert syncs.value.state is ProviderSyncState.FAILED
    assert syncs.value.last_error == "ConnectionError"

    immediate_retry = await _reconciler(_device(), provider=provider, syncs=syncs).execute()
    assert immediate_retry.skipped == 1
    assert len(provider.contexts) == 1


async def test_last_connection_loss_reconciles_empty_assignments_and_removes_stale_lease() -> None:
    provider, syncs, leases = _Provider(), _Syncs(), _Leases()
    connections = _Connections(active=True)
    reconciler = _reconciler(
        _device(), connections=connections, provider=provider, syncs=syncs, leases=leases
    )
    await reconciler.execute()
    connections.active = False

    result = await reconciler.execute()

    assert result.succeeded == 1
    assert provider.contexts[-1].connected is False
    assert leases.values == {}
    assert (await reconciler.execute()).skipped == 1


async def test_pending_grant_is_reissued_after_delivery_window_for_restart_recovery() -> None:
    provider, syncs, leases, sender = _Provider(), _Syncs(), _Leases(), _GrantSender()
    clock = _Clock()
    reconciler = ReconcileDeviceChannels(
        hub_id="hub-local",
        hub_instance_id="hub-local-1",
        devices=_Devices(_device()),
        connections=_Connections(),
        syncs=syncs,
        channel_leases=leases,
        provider=provider,
        grant_sender=sender,
        clock=clock,
    )
    await reconciler.execute()
    clock.value = NOW + timedelta(seconds=10)
    assert (await reconciler.execute()).skipped == 1
    clock.value = NOW + timedelta(seconds=31)

    retried = await reconciler.execute()

    assert retried.succeeded == 1
    assert len(sender.sent) == 2


async def test_provider_rejects_available_device_without_management_data_assignment() -> None:
    class _EmptyProvider:
        async def sync_device(self, context):
            return ChannelAssignmentSet(
                context.operation_id, context.device_id, context.manifest_revision, ()
            )

    syncs = _Syncs()

    result = await _reconciler(_device(), provider=_EmptyProvider(), syncs=syncs).execute()

    assert result.failed == 1
    assert syncs.value.last_error == "ValueError"


async def test_database_claim_owned_by_another_instance_is_skipped() -> None:
    class _OwnedSyncs(_Syncs):
        async def try_claim(self, desired, *, owner_instance_id, now, claim_ttl):
            return None

    provider = _Provider()

    result = await _reconciler(_device(), provider=provider, syncs=_OwnedSyncs()).execute()

    assert result.skipped == 1
    assert provider.contexts == []


async def test_mismatched_provider_response_is_failed_without_persisting_binding() -> None:
    class _MismatchedProvider(_Provider):
        async def sync_device(self, context):
            response = await super().sync_device(context)
            return replace(response, operation_id="another-operation")

    syncs, leases = _Syncs(), _Leases()

    result = await _reconciler(
        _device(), provider=_MismatchedProvider(), syncs=syncs, leases=leases
    ).execute()

    assert result.failed == 1
    assert syncs.value.last_error == "ValueError"
    assert leases.values == {}


async def test_signaling_failure_restores_previous_active_assignment() -> None:
    provider, syncs, leases = _Provider(), _Syncs(), _Leases()
    prior = ChannelLease(
        channel_id="channel-1",
        device_id="device-1",
        purpose="management",
        kinds=frozenset({ChannelKind.RELIABLE_DATA}),
        binding_format="application/old-provider+json",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
        state=ChannelState.ACTIVE,
    )
    await leases.upsert(prior)

    result = await _reconciler(
        _device(),
        provider=provider,
        syncs=syncs,
        leases=leases,
        sender=_GrantSender(ConnectionError("mailbox full")),
    ).execute()

    assert result.failed == 1
    assert leases.values["channel-1"] == prior


async def test_active_assignment_is_converged_without_another_provider_call() -> None:
    provider, syncs, leases = _Provider(), _Syncs(), _Leases()
    reconciler = _reconciler(_device(), provider=provider, syncs=syncs, leases=leases)
    await reconciler.execute()
    leases.values["channel-1"] = leases.values["channel-1"].transition(
        ChannelState.ACTIVE, occurred_at=NOW
    )

    result = await reconciler.execute()

    assert result.skipped == 1
    assert len(provider.contexts) == 1


async def test_assignment_near_expiry_uses_a_new_idempotency_generation() -> None:
    clock = _Clock()

    class _RenewingProvider(_Provider):
        async def sync_device(self, context):
            self.contexts.append(context)
            return ChannelAssignmentSet(
                operation_id=context.operation_id,
                device_id=context.device_id,
                manifest_revision=context.manifest_revision,
                grants=(
                    ChannelGrant(
                        operation_id=context.operation_id,
                        lease=ChannelLease(
                            channel_id=f"channel-{len(self.contexts)}",
                            device_id=context.device_id,
                            purpose="management",
                            kinds=frozenset({ChannelKind.RELIABLE_DATA}),
                            binding_format="application/eidolon-test+json",
                            issued_at=clock.value,
                            expires_at=clock.value + timedelta(minutes=5),
                        ),
                        opaque_binding=OpaqueChannelBinding(b"rotated-provider-binding"),
                    ),
                ),
            )

    provider, syncs, leases = _RenewingProvider(), _Syncs(), _Leases()
    reconciler = ReconcileDeviceChannels(
        hub_id="hub-local",
        hub_instance_id="hub-local-1",
        devices=_Devices(_device()),
        connections=_Connections(),
        syncs=syncs,
        channel_leases=leases,
        provider=provider,
        grant_sender=_GrantSender(),
        clock=clock,
    )
    await reconciler.execute()
    first_operation = provider.contexts[0].operation_id
    leases.values["channel-1"] = leases.values["channel-1"].transition(
        ChannelState.ACTIVE, occurred_at=NOW
    )
    clock.value = NOW + timedelta(minutes=4, seconds=31)

    result = await reconciler.execute()

    assert result.succeeded == 1
    assert provider.contexts[-1].operation_id != first_operation
    assert set(leases.values) == {"channel-2"}


async def test_unaccepted_grant_rotates_after_its_usable_lifetime() -> None:
    clock = _Clock()

    class _RenewingProvider(_Provider):
        async def sync_device(self, context):
            self.contexts.append(context)
            return ChannelAssignmentSet(
                context.operation_id,
                context.device_id,
                context.manifest_revision,
                (
                    ChannelGrant(
                        context.operation_id,
                        ChannelLease(
                            channel_id=f"channel-{len(self.contexts)}",
                            device_id=context.device_id,
                            purpose="management",
                            kinds=frozenset({ChannelKind.RELIABLE_DATA}),
                            binding_format="application/eidolon-test+json",
                            issued_at=clock.value,
                            expires_at=clock.value + timedelta(minutes=5),
                        ),
                        OpaqueChannelBinding(b"provider-binding"),
                    ),
                ),
            )

    provider, syncs, leases = _RenewingProvider(), _Syncs(), _Leases()
    reconciler = ReconcileDeviceChannels(
        hub_id="hub-local",
        hub_instance_id="hub-local-1",
        devices=_Devices(_device()),
        connections=_Connections(),
        syncs=syncs,
        channel_leases=leases,
        provider=provider,
        grant_sender=_GrantSender(),
        clock=clock,
    )
    await reconciler.execute()
    first_operation = provider.contexts[0].operation_id
    clock.value = NOW + timedelta(minutes=4, seconds=31)

    result = await reconciler.execute()

    assert result.succeeded == 1
    assert provider.contexts[-1].operation_id != first_operation
    assert set(leases.values) == {"channel-2"}


async def test_provider_grant_must_outlive_the_refresh_window() -> None:
    provider, syncs = _Provider(), _Syncs()
    clock = _Clock()
    clock.value = NOW + timedelta(minutes=4, seconds=31)
    reconciler = ReconcileDeviceChannels(
        hub_id="hub-local",
        hub_instance_id="hub-local-1",
        devices=_Devices(_device()),
        connections=_Connections(),
        syncs=syncs,
        channel_leases=_Leases(),
        provider=provider,
        grant_sender=_GrantSender(),
        clock=clock,
    )

    result = await reconciler.execute()

    assert result.failed == 1
    assert syncs.value.last_error == "ValueError"


async def test_owner_transfer_changes_desired_state_and_provider_context() -> None:
    devices = _Devices(_device(owner_id="owner-1"))
    provider, syncs, leases = _Provider(), _Syncs(), _Leases()
    reconciler = ReconcileDeviceChannels(
        hub_id="hub-local",
        hub_instance_id="hub-local-1",
        devices=devices,
        connections=_Connections(),
        syncs=syncs,
        channel_leases=leases,
        provider=provider,
        grant_sender=_GrantSender(),
        clock=_Clock(),
    )
    await reconciler.execute()
    first_operation = provider.contexts[0].operation_id
    devices.device = replace(devices.device, owner_id="owner-2")

    result = await reconciler.execute()

    assert result.succeeded == 1
    assert provider.contexts[-1].owner_id == "owner-2"
    assert provider.contexts[-1].operation_id != first_operation
