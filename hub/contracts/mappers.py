"""Explicit mappings between wire DTOs and domain entities."""

from __future__ import annotations

from pydantic import ValidationError

from hub.contracts.bindings.device import (
    DeviceDirectoryEntry as DeviceDirectoryEntryWire,
)
from hub.contracts.bindings.device import (
    DeviceLifecycleStatus,
    DeviceManagementEvent,
    DeviceManifest,
    ForeignDeviceManifest,
)
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    ManagedDevice,
)
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import StoredDeviceManagementEvent


def manifest_to_wire(
    document: DeviceManifestDocument,
) -> DeviceManifest | ForeignDeviceManifest:
    """Read a stored Manifest, and say so plainly when this vocabulary cannot.

    The Authority accepted this document at some point, possibly under an entry
    that checked less than today's does; the entry is where requiredness lives,
    because that is where a device is still asking and can still be answered.
    Here the document is already history, and a reader of history that can raise
    is a reader that can take an Owner's device page down for a device Hub
    itself admitted. So the failure is projected, never propagated.
    """

    try:
        return DeviceManifest.model_validate_json(document.canonical_json)
    except ValidationError as error:
        return ForeignDeviceManifest(detail=_unreadable_because(error))


def _unreadable_because(error: ValidationError) -> str:
    """State the mismatch in field terms, without leaking a stack of internals."""

    reasons = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'document'}: {item['msg']}"
        for item in error.errors(include_url=False)[:8]
    )
    if not reasons:
        return "the stored document does not match this vocabulary"
    return reasons if len(reasons) <= 512 else reasons[:511] + "…"


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
        manifest=manifest_to_wire(entry.manifest),
        manifest_revision=entry.manifest_digest,
        lifecycle_state=entry.lifecycle_state.value,
        enrolled_at=entry.enrolled_at,
        updated_at=entry.updated_at,
        device_ref=entry.device_ref,
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
