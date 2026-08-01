from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.domain.channels.entities import (
    ChannelDataEnvelope,
    ChannelKind,
    ChannelLease,
    ChannelNegotiationIntent,
    ChannelNegotiationOperation,
    ChannelProfile,
    ChannelRequest,
    DeviceEventData,
    OpaqueChannelBinding,
)
from hub.domain.channels.selection import ChannelProfileCatalog, ChannelProfileUnavailable
from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.connections.entities import (
    ConnectionLease,
    ConnectorKind,
    DeviceAuthorityLease,
)
from hub.domain.connections.registry import ConnectionNotFound, ConnectionRegistry
from hub.domain.devices.capabilities import BODY_OP_PRESENCE_SET
from hub.domain.devices.entities import DeviceRegistrationIntent, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _profile() -> ChannelProfile:
    return ChannelProfile(
        "management-data",
        frozenset({ChannelKind.RELIABLE_DATA}),
        "provider/default",
    )


def _channel_lease() -> ChannelLease:
    return ChannelLease(
        channel_id="channel-1",
        device_id="device-1",
        profile_name="management-data",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )


def _connection(**changes) -> ConnectionLease:
    values = {
        "connection_id": "connection-1",
        "device_id": "device-1",
        "connector_id": "https-local",
        "connector_kind": ConnectorKind.HTTPS,
        "signaling_ref": "http-mailbox:device-1",
        "opened_at": NOW,
        "renewed_at": NOW,
        "expires_at": NOW + timedelta(seconds=45),
        "lease_token": "lease-token-device-1",
        "identity_fingerprint": "p256:fingerprint",
        "hub_instance_id": "hub-1",
        "fencing_token": 1,
    }
    values.update(changes)
    return ConnectionLease(**values)


def _manifest() -> DeviceManifestDocument:
    return DeviceManifestDocument.from_mapping({"schema_version": 1, "title": "Device"})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": " "}, "name"),
        ({"required_kinds": frozenset()}, "at least one"),
        ({"provisioner_ref": " "}, "provisioner_ref"),
    ],
)
def test_channel_profile_invariants(changes, message) -> None:
    values = {
        "name": "management-data",
        "required_kinds": frozenset({ChannelKind.RELIABLE_DATA}),
        "provisioner_ref": "provider/default",
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ChannelProfile(**values)


def test_channel_request_and_binding_invariants() -> None:
    base = ChannelRequest("request-1", "device-1", _profile(), NOW, NOW + timedelta(seconds=5))
    assert base.request_id == "request-1"

    for changes, message in (
        ({"request_id": ""}, "required"),
        ({"requested_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
        ({"expires_at": NOW}, "expire after"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(base, **changes)
    with pytest.raises(ValueError, match="required"):
        OpaqueChannelBinding(b"")
    with pytest.raises(ValueError, match="64KiB"):
        OpaqueChannelBinding(b"x" * (64 * 1024 + 1))
    binding = OpaqueChannelBinding(b"encrypted")
    assert binding.relay_bytes() == b"encrypted"


def test_channel_lease_envelope_and_signal_invariants() -> None:
    lease = _channel_lease()
    for changes, message in (
        ({"channel_id": ""}, "required"),
        ({"issued_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
        ({"expires_at": NOW}, "after issued_at"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(lease, **changes)

    envelope = ChannelDataEnvelope(
        envelope_id="envelope-1",
        channel_id="channel-1",
        device_id="device-1",
        sequence=1,
        occurred_at=NOW,
        payload=DeviceEventData("event-1", "button", "{}"),
    )
    for changes, message in (
        ({"envelope_id": ""}, "identifiers"),
        ({"sequence": 0}, "positive"),
        ({"occurred_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(envelope, **changes)

    offer = ChannelNegotiationIntent(
        ChannelNegotiationOperation.OFFER,
        "signal-1",
        "device-1",
        "connection-1",
    )
    assert offer.channel_id is None
    with pytest.raises(ValueError, match="identifiers"):
        replace(offer, request_id="")
    with pytest.raises(ValueError, match="channel_id"):
        replace(offer, operation=ChannelNegotiationOperation.ACCEPT)


def test_channel_profile_catalog_failure_paths() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ChannelProfileCatalog(())
    catalog = ChannelProfileCatalog((_profile(),))
    with pytest.raises(ChannelProfileUnavailable):
        catalog.get("missing")
    with pytest.raises(ChannelProfileUnavailable):
        catalog.select(frozenset({ChannelKind.AUDIO}))


def test_authority_and_connection_invariants_and_renewal_guards() -> None:
    authority = DeviceAuthorityLease("device-1", "hub-1", 1, NOW + timedelta(seconds=45))
    for changes, message in (
        ({"device_id": ""}, "required"),
        ({"fencing_token": 0}, "positive"),
        ({"expires_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(authority, **changes)

    lease = _connection()
    for field_name in (
        "connection_id",
        "device_id",
        "connector_id",
        "signaling_ref",
        "lease_token",
        "identity_fingerprint",
        "hub_instance_id",
    ):
        with pytest.raises(ValueError, match=field_name):
            replace(lease, **{field_name: ""})
    for field_name in ("opened_at", "renewed_at", "expires_at"):
        with pytest.raises(ValueError, match="timezone-aware"):
            replace(lease, **{field_name: NOW.replace(tzinfo=None)})
    for changes, message in (
        ({"fencing_token": 0}, "positive"),
        ({"heartbeat_sequence": -1}, "negative"),
        ({"expires_at": NOW}, "after renewed_at"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(lease, **changes)

    with pytest.raises(ValueError, match="closed"):
        lease.close().renew(now=NOW, ttl=timedelta(seconds=1), lease_token=lease.lease_token)
    with pytest.raises(PermissionError, match="mismatch"):
        lease.renew(now=NOW, ttl=timedelta(seconds=1), lease_token="wrong")
    with pytest.raises(ValueError, match="positive"):
        lease.renew(now=NOW, ttl=timedelta(0), lease_token=lease.lease_token)
    sequenced = lease.renew(
        now=NOW + timedelta(seconds=1),
        ttl=timedelta(seconds=10),
        lease_token=lease.lease_token,
        sequence=2,
    )
    with pytest.raises(ValueError, match="stale"):
        sequenced.renew(
            now=NOW, ttl=timedelta(seconds=1), lease_token=lease.lease_token, sequence=1
        )
    assert (
        sequenced.renew(
            now=NOW, ttl=timedelta(seconds=1), lease_token=lease.lease_token, sequence=2
        )
        is sequenced
    )
    assert (
        lease.renew(
            now=NOW, ttl=timedelta(seconds=1), lease_token=lease.lease_token
        ).heartbeat_sequence
        == 1
    )


def test_connection_registry_conflicts_not_found_and_idempotency() -> None:
    registry = ConnectionRegistry()
    original = registry.open(_connection())
    assert registry.open(_connection()) is original
    with pytest.raises(ValueError, match="another device"):
        registry.open(_connection(device_id="device-2"))
    replaced = registry.open(_connection(fencing_token=2, priority=1))
    assert replaced.fencing_token == 2
    renewed = registry.renew(
        "connection-1", lease_token=replaced.lease_token, ttl=timedelta(seconds=30), now=NOW
    )
    assert renewed.heartbeat_sequence == 1
    for operation in (
        lambda: registry.renew("missing", lease_token="x", ttl=timedelta(seconds=1), now=NOW),
        lambda: registry.close("missing"),
    ):
        with pytest.raises(ConnectionNotFound):
            operation()
    assert ConnectionRegistry().preferred_for_device("missing", now=NOW) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": ""},
        {"public_key_fingerprint": ""},
        {"tenant_id": ""},
    ],
)
def test_device_identity_requires_stable_nonempty_values(changes) -> None:
    values = {
        "device_id": "device-1",
        "public_key_fingerprint": "p256:fingerprint",
        "tenant_id": "local",
    }
    values.update(changes)
    with pytest.raises(ValueError):
        DeviceIdentity(**values)


def test_manifest_document_is_canonical_and_content_addressed() -> None:
    manifest = _manifest()
    assert manifest.canonical_json == '{"schema_version":1,"title":"Device"}'
    with pytest.raises(ValueError, match="object"):
        DeviceManifestDocument.from_mapping([])
    with pytest.raises(ValueError, match="valid JSON"):
        DeviceManifestDocument("{", "sha256:bad")
    with pytest.raises(ValueError, match="schema_version"):
        DeviceManifestDocument.from_mapping({"schema_version": 2})
    with pytest.raises(ValueError, match="revision"):
        DeviceManifestDocument(manifest.canonical_json, "sha256:bad")


def test_managed_device_registration_and_command_values() -> None:
    identity = DeviceIdentity("device-1", "p256:fingerprint")
    manifest = _manifest()
    device = ManagedDevice(identity, "Device", "generic", manifest, NOW, NOW)
    assert device.manifest_json == manifest.canonical_json
    assert device.manifest_revision == manifest.revision
    intent = DeviceRegistrationIntent("request-1", identity, "Device", "generic", manifest)
    assert intent.manifest_json == manifest.canonical_json
    assert intent.manifest_revision == manifest.revision
    assert BODY_OP_PRESENCE_SET == "body.presence.set"
    with pytest.raises(ValueError, match="device_kind"):
        replace(device, device_kind="")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(device, updated_at=NOW.replace(tzinfo=None))

    command = DeviceCommand(
        "command-1",
        "device-1",
        "display.render",
        "{}",
        CommandState.QUEUED,
        NOW,
        NOW + timedelta(seconds=1),
        NOW,
    )
    assert command.terminal is False
    for changes, message in (
        ({"command_id": ""}, "required"),
        ({"updated_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
        ({"expires_at": NOW}, "expire after"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(command, **changes)
    same = command.with_state(CommandState.SENT, at=NOW, error="", result_json=None)
    assert same.state is CommandState.SENT
