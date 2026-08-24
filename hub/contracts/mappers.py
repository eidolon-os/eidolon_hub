"""Explicit mappings between wire DTOs and domain entities."""

from __future__ import annotations

import base64
import hashlib
import json

from hub.contracts.bindings.channel import ChannelAssignment
from hub.contracts.bindings.device import (
    DeviceControlOperationStatus,
    DeviceLifecycleStatus,
    DeviceManagementEvent,
    DeviceManifest,
    LegacyClaimEvent,
    LegacyClaimRevocationResult,
    LegacyDeviceRef,
)
from hub.contracts.bindings.device import (
    DeviceDirectoryEntry as DeviceDirectoryEntryWire,
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
from hub.ports.claim_lifecycle import ClaimCommandResult, StoredClaimEvent
from hub.ports.device_control import DeviceControlOperation
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
        device_ref=(
            LegacyDeviceRef(
                device_instance_id=device.device_ref.device_instance_id,
                # PH2-B removes this legacy projection, whose field name
                # historically carried the business Owner id.
                owner_domain_id=device.owner_id,
                owner_domain_generation=device.device_ref.owner_domain_generation,
                claim_generation=device.device_ref.claim_generation,
                trust_epoch=device.device_ref.trust_epoch,
                accepted_manifest_digest=device.manifest_revision,
            )
            if device.lifecycle_state.value != "pending-approval" and device.device_ref is not None
            else None
        ),
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
        device_ref=(
            LegacyDeviceRef(
                device_instance_id=entry.device_ref.device_instance_id,
                owner_domain_id=entry.owner_scope,
                owner_domain_generation=entry.device_ref.owner_domain_generation,
                claim_generation=entry.device_ref.claim_generation,
                trust_epoch=entry.device_ref.trust_epoch,
                accepted_manifest_digest=entry.manifest_revision,
            )
            if entry.lifecycle_state.value != "pending-approval"
            else None
        ),
    )


def claim_result_to_wire(
    result: ClaimCommandResult, *, business_owner_id: str, manifest_digest: str
) -> LegacyClaimRevocationResult:
    return LegacyClaimRevocationResult(
        command_id=result.command_id,
        outcome=result.outcome,
        device_ref=LegacyDeviceRef(
            device_instance_id=result.device_ref.device_instance_id,
            owner_domain_id=business_owner_id,
            owner_domain_generation=result.device_ref.owner_domain_generation,
            claim_generation=result.device_ref.claim_generation,
            trust_epoch=result.device_ref.trust_epoch,
            accepted_manifest_digest=manifest_digest,
        ),
        aggregate_revision=result.aggregate_revision,
        occurred_at=result.occurred_at,
        event_id=result.event_id,
    )


def stored_claim_event_to_wire(stored: StoredClaimEvent) -> LegacyClaimEvent:
    event = stored.event
    return LegacyClaimEvent(
        stream_position=stored.stream_position,
        event_id=event.event_id,
        event_type=event.event_type,
        device_ref=LegacyDeviceRef(
            device_instance_id=event.device_ref.device_instance_id,
            owner_domain_id=event.business_owner_id,
            owner_domain_generation=event.device_ref.owner_domain_generation,
            claim_generation=event.device_ref.claim_generation,
            trust_epoch=event.device_ref.trust_epoch,
            accepted_manifest_digest=event.manifest_digest,
        ),
        aggregate_revision=event.aggregate_revision,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at,
        reason=event.reason,
    )


def device_control_operation_to_wire(
    operation: DeviceControlOperation,
) -> DeviceControlOperationStatus:
    return DeviceControlOperationStatus(
        event_id=operation.event_id,
        operation_id=operation.operation_id,
        operation_type=operation.operation_type,
        device_ref=operation.device_ref,
        state=operation.state,
        attempt_count=operation.attempt_count,
        next_attempt_at=operation.next_attempt_at,
        delivered_at=operation.delivered_at,
        last_error=operation.last_error,
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
