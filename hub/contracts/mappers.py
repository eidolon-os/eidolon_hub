"""Explicit mappings between wire DTOs and domain entities."""

from __future__ import annotations

import base64
import hashlib
import json

from hub.contracts.bindings.channel import ChannelAssignment
from hub.contracts.bindings.device import (
    DeviceDirectoryEntry as DeviceDirectoryEntryWire,
)
from hub.contracts.bindings.device import (
    DeviceLifecycleStatus,
    DeviceManagementEvent,
    DeviceManifest,
)
from hub.contracts.bindings.onboarding import (
    DeviceEnrollment,
    DeviceEnrollmentReceipt,
    DeviceHandoffOutcome,
)
from hub.domain.channels.entities import ChannelAssignmentSet
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    DeviceEnrollmentIntent,
    ManagedDevice,
)
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import StoredDeviceManagementEvent


def canonical_manifest(enrollment: DeviceEnrollment) -> str:
    return json.dumps(
        enrollment.manifest.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def manifest_revision(manifest_json: str) -> str:
    return "sha256:" + hashlib.sha256(manifest_json.encode()).hexdigest()


def enrollment_to_domain(enrollment: DeviceEnrollment) -> DeviceEnrollmentIntent:
    manifest_json = canonical_manifest(enrollment)
    return DeviceEnrollmentIntent(
        request_id=enrollment.request_id,
        retrieval_token=enrollment.retrieval_token,
        identity=DeviceIdentity(device_id=enrollment.identity.device_id),
        display_name=enrollment.display_name,
        device_kind=enrollment.device_kind,
        manifest=DeviceManifestDocument(
            canonical_json=manifest_json,
            revision=manifest_revision(manifest_json),
        ),
    )


def enrollment_receipt_to_wire(
    device: ManagedDevice, *, request_id: str
) -> DeviceEnrollmentReceipt:
    return DeviceEnrollmentReceipt(
        request_id=request_id,
        enrollment_id=device.enrollment_id,
        device_id=device.identity.device_id,
        lifecycle_state=device.lifecycle_state.value,
        retrieval_expires_at_ms=int(device.retrieval_expires_at.timestamp() * 1000),
    )


def handoff_outcome_to_wire(
    device: ManagedDevice,
    assignments: ChannelAssignmentSet | None,
    *,
    request_id: str,
) -> DeviceHandoffOutcome:
    return DeviceHandoffOutcome(
        request_id=request_id,
        enrollment_id=device.enrollment_id,
        device_id=device.identity.device_id,
        manifest_revision=device.manifest_revision,
        lifecycle_state=device.lifecycle_state.value,
        channels=tuple(
            ChannelAssignment(
                channel_id=grant.channel_id,
                purpose=grant.purpose,
                kinds=tuple(sorted(kind.value for kind in grant.kinds)),
                binding_format=grant.binding_format,
                issued_at_ms=int(grant.issued_at.timestamp() * 1000),
                expires_at_ms=int(grant.expires_at.timestamp() * 1000),
                opaque_binding=base64.b64encode(grant.opaque_binding.relay_bytes()).decode("ascii"),
            )
            for grant in (assignments.grants if assignments is not None else ())
        ),
    )


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
    )


def stored_event_to_wire(stored: StoredDeviceManagementEvent) -> DeviceManagementEvent:
    event = stored.event
    return DeviceManagementEvent(
        stream_position=stored.stream_position,
        event_id=event.event_id,
        event_type=event.event_type,
        source=event.source,
        device_id=event.subject,
        occurred_at=event.occurred_at,
        data=event.data,
    )
