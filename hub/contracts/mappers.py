"""Explicit mappings between wire DTOs and domain entities."""

from __future__ import annotations

from hub.contracts.bindings.device import (
    DeviceDirectoryEntry as DeviceDirectoryEntryWire,
)
from hub.contracts.bindings.device import (
    DeviceLifecycleStatus,
    DeviceManagementEvent,
    DeviceManifest,
    DeviceRef,
)
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    ManagedDevice,
)
from hub.ports.management_events import StoredDeviceManagementEvent


def lifecycle_status_to_wire(device: ManagedDevice) -> DeviceLifecycleStatus:
    return DeviceLifecycleStatus(
        device_id=device.identity.device_id,
        owner_id=device.owner_id,
        lifecycle_state=device.lifecycle_state.value,
    )


def directory_entry_to_wire(entry: DeviceDirectoryEntry) -> DeviceDirectoryEntryWire:
    return DeviceDirectoryEntryWire(
        device_id=entry.device_id,
        owner_scope=entry.owner_scope,
        display_name=entry.display_name,
        device_kind=entry.device_kind,
        manifest=DeviceManifest.model_validate_json(entry.manifest.canonical_json),
        manifest_revision=entry.manifest_revision,
        lifecycle_state=entry.lifecycle_state.value,
        enrolled_at=entry.enrolled_at,
        updated_at=entry.updated_at,
        device_ref=DeviceRef(
            device_instance_id=entry.device_ref.device_instance_id,
            owner_domain_id=entry.owner_scope,
            owner_domain_generation=entry.device_ref.owner_domain_generation,
            claim_generation=entry.device_ref.claim_generation,
            trust_epoch=entry.device_ref.trust_epoch,
        ),
    )


def stored_event_to_wire(stored: StoredDeviceManagementEvent) -> DeviceManagementEvent:
    event = stored.event
    return DeviceManagementEvent(
        stream_position=stored.stream_position,
        event_id=event.event_id,
        event_type=event.event_type,
        source=event.source,
        principal_id=event.principal_id,
        device_id=event.subject,
        occurred_at=event.occurred_at,
        data=event.data,
    )
