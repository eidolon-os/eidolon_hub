"""Explicit mappings between generated wire DTOs and domain entities."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from hub.contracts.bindings.channel import (
    ChannelLifecycleEvent,
    CommandAckPayload,
    CommandResultPayload,
    DataEnvelope,
    DeviceEventPayload,
    ReportedStatePayload,
)
from hub.contracts.bindings.device import (
    DeviceBusEvent,
    DeviceCommandStatus,
    DeviceLifecycleStatus,
    DeviceRegistration,
    DeviceRegistrationStatus,
)
from hub.contracts.bindings.device import (
    DeviceDirectoryEntry as DeviceDirectoryEntryWire,
)
from hub.contracts.bindings.device import (
    DirectoryConnection as DirectoryConnectionWire,
)
from hub.domain.channels.entities import (
    ChannelDataEnvelope,
    ChannelLifecycle,
    ChannelState,
    CommandAckData,
    CommandResultData,
    DeviceEventData,
    InboundCommandData,
    ReportedStateData,
)
from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    DeviceRegistrationIntent,
    ManagedDevice,
)
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.event_bus import StoredDomainEvent


def identity_to_domain(registration: DeviceRegistration) -> DeviceIdentity:
    value = registration.identity
    return DeviceIdentity(
        device_id=value.device_id,
        public_key_fingerprint=value.public_key_fingerprint,
        tenant_id=value.tenant_id,
    )


def canonical_manifest(registration: DeviceRegistration) -> str:
    return json.dumps(
        registration.manifest.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def manifest_revision(manifest_json: str) -> str:
    return "sha256:" + hashlib.sha256(manifest_json.encode()).hexdigest()


def registration_to_domain(
    registration: DeviceRegistration,
) -> DeviceRegistrationIntent:
    identity = identity_to_domain(registration)
    manifest_json = canonical_manifest(registration)
    return DeviceRegistrationIntent(
        request_id=registration.request_id,
        identity=identity,
        display_name=registration.display_name,
        device_kind=registration.device_kind,
        manifest=DeviceManifestDocument(
            canonical_json=manifest_json,
            revision=manifest_revision(manifest_json),
        ),
    )


def data_envelope_to_domain(envelope: DataEnvelope) -> ChannelDataEnvelope:
    if envelope.kind == "command":
        payload = InboundCommandData()
    elif envelope.kind == "ack":
        value = CommandAckPayload.model_validate_json(envelope.payload_json)
        payload = CommandAckData(
            command_id=value.command_id,
            target_state={
                "accepted": CommandState.ACCEPTED,
                "running": CommandState.RUNNING,
                "rejected": CommandState.REJECTED,
            }[value.status],
            error=value.error,
        )
    elif envelope.kind == "result":
        value = CommandResultPayload.model_validate_json(envelope.payload_json)
        payload = CommandResultData(
            command_id=value.command_id,
            target_state={
                "succeeded": CommandState.SUCCEEDED,
                "failed": CommandState.FAILED,
                "rejected": CommandState.REJECTED,
                "expired": CommandState.EXPIRED,
            }[value.status],
            result_json=value.result_json,
            error=value.error,
        )
    elif envelope.kind == "state":
        value = ReportedStatePayload.model_validate_json(envelope.payload_json)
        payload = ReportedStateData(
            revision=value.revision,
            values_json=value.values_json,
        )
    else:
        value = DeviceEventPayload.model_validate_json(envelope.payload_json)
        payload = DeviceEventData(
            event_id=value.event_id,
            name=value.name,
            payload_json=value.payload_json,
        )
    return ChannelDataEnvelope(
        envelope_id=envelope.envelope_id,
        channel_id=envelope.channel_id,
        device_id=envelope.device_id,
        sequence=envelope.sequence,
        occurred_at=datetime.fromtimestamp(envelope.occurred_at_ms / 1_000, tz=UTC),
        payload=payload,
    )


def channel_lifecycle_to_domain(event: ChannelLifecycleEvent) -> ChannelLifecycle:
    return ChannelLifecycle(
        channel_id=event.channel_id,
        device_id=event.device_id,
        state=ChannelState(event.state),
        occurred_at=datetime.fromtimestamp(event.occurred_at_ms / 1_000, tz=UTC),
        reason=event.reason,
    )


def registration_status_to_wire(device: ManagedDevice) -> DeviceRegistrationStatus:
    return DeviceRegistrationStatus(
        device_id=device.identity.device_id,
        manifest_revision=device.manifest_revision,
        approved=device.approved,
    )


def lifecycle_status_to_wire(device: ManagedDevice) -> DeviceLifecycleStatus:
    return DeviceLifecycleStatus(
        device_id=device.identity.device_id,
        owner_id=device.owner_id,
        approved=device.approved,
        revoked=device.revoked,
    )


def directory_entry_to_wire(entry: DeviceDirectoryEntry) -> DeviceDirectoryEntryWire:
    return DeviceDirectoryEntryWire(
        device_id=entry.device_id,
        owner_scope=entry.owner_scope,
        display_name=entry.display_name,
        device_kind=entry.device_kind,
        manifest_json=entry.manifest_json,
        manifest_revision=entry.manifest_revision,
        approved=entry.approved,
        revoked=entry.revoked,
        online=entry.online,
        connections=tuple(
            DirectoryConnectionWire(
                connection_id=value.connection_id,
                connector_id=value.connector_id,
                connector_kind=value.connector_kind,
                expires_at=value.expires_at,
            )
            for value in entry.connections
        ),
        registered_at=entry.registered_at,
        updated_at=entry.updated_at,
        revision=entry.revision,
    )


def command_status_to_wire(command: DeviceCommand) -> DeviceCommandStatus:
    return DeviceCommandStatus(
        command_id=command.command_id,
        device_id=command.device_id,
        command_name=command.operation,
        state=command.state.value,
        created_at=command.created_at,
        updated_at=command.updated_at,
        expires_at=command.expires_at,
        error=command.error,
        result_json=command.result_json,
    )


def stored_event_to_wire(stored: StoredDomainEvent) -> DeviceBusEvent:
    event = stored.event
    return DeviceBusEvent(
        stream_position=stored.stream_position,
        event_id=event.event_id,
        event_type=event.event_type,
        source=event.source,
        device_id=event.subject,
        occurred_at=event.occurred_at,
        data_json=event.data_json,
    )
