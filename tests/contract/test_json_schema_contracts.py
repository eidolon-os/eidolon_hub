from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from hub.contracts.bindings.channel import ChannelAssignment
from hub.contracts.bindings.device import (
    DeviceDirectoryEntry,
    DeviceDirectoryPage,
    DeviceLifecycleStatus,
    DeviceManagementEvent,
    DeviceManifest,
)
from hub.contracts.bindings.onboarding import (
    DeviceEnrollment,
    DeviceEnrollmentReceipt,
    DeviceHandoffOutcome,
    DeviceHandoffRequest,
    HubDescriptor,
)
from hub.contracts.generated.schema_models.device.manifest_schema import (
    DeviceManifest as GeneratedDeviceManifest,
)
from hub.contracts.generated.schema_models.onboarding.handoff_schema import (
    DeviceHandoffOutcome as GeneratedDeviceHandoffOutcome,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "hub" / "contracts" / "schemas"
EXAMPLES = ROOT / "hub" / "contracts" / "examples"
NOW = datetime(2026, 8, 4, tzinfo=UTC)


def _schema(relative: str):
    return json.loads((SCHEMAS / relative).read_text(encoding="utf-8"))


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validate(relative: str, model) -> None:
    Draft202012Validator(_schema(relative), registry=_registry()).validate(
        model.model_dump(mode="json")
    )


def test_all_contract_schemas_are_valid_draft_2020_12() -> None:
    for path in SCHEMAS.rglob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_generated_contract_shapes_are_current() -> None:
    subprocess.run(
        [sys.executable, "scripts/generate_contracts.py", "--check"], cwd=ROOT, check=True
    )


def test_runtime_bindings_conform_to_generated_shapes() -> None:
    manifest = DeviceManifest(title="Generic Device")
    outcome = DeviceHandoffOutcome(
        request_id="handoff-1",
        enrollment_id="enrollment-1",
        device_id="device-1",
        manifest_revision="sha256:revision",
        lifecycle_state="approved",
        channels=(
            ChannelAssignment(
                channel_id="channel-1",
                purpose="provider-selected",
                kinds=("reliable-data",),
                binding_format="application/test+json",
                issued_at_ms=1,
                expires_at_ms=2,
                opaque_binding="b3BhcXVl",
            ),
        ),
    )

    GeneratedDeviceManifest.model_validate(manifest.model_dump(mode="json"))
    GeneratedDeviceHandoffOutcome.model_validate(outcome.model_dump(mode="json"))


def test_public_status_bindings_conform_to_schema_sources() -> None:
    values = (
        (
            "onboarding/descriptor.schema.json",
            HubDescriptor(
                hub_id="hub-1",
                descriptor_uri="https://hub.example/api/device-onboarding/v1/descriptor",
                device_onboarding_uri="https://hub.example/api/device-onboarding/v1",
                enrollment_uri="https://hub.example/api/device-onboarding/v1/enrollments",
            ),
        ),
        (
            "device/directory.schema.json",
            DeviceDirectoryEntry(
                device_id="device-1",
                owner_scope="owner-1",
                display_name="Device",
                device_kind="generic",
                manifest=DeviceManifest(title="Device"),
                manifest_revision="sha256:revision",
                lifecycle_state="approved",
                enrolled_at=NOW,
                updated_at=NOW,
            ),
        ),
        ("device/directory-page.schema.json", DeviceDirectoryPage()),
        (
            "device/status.schema.json",
            DeviceLifecycleStatus(
                device_id="device-1", owner_id="owner-1", lifecycle_state="approved"
            ),
        ),
        (
            "device/management-event.schema.json",
            DeviceManagementEvent(
                stream_position=1,
                event_id="event-1",
                event_type="eidolon.device.enrolled.v1",
                source="eidolon-hub/device-management",
                device_id="device-1",
                occurred_at=NOW,
                data={"manifest_revision": "sha256:revision"},
            ),
        ),
    )
    for relative, model in values:
        _validate(relative, model)


def test_enrollment_request_and_receipt_conform_to_same_contract() -> None:
    values = (
        DeviceEnrollment(
            request_id="enroll-1",
            retrieval_token="device-generated-random-token-000001",
            identity={"device_id": "device-1"},
            manifest=DeviceManifest(title="Device"),
        ),
        DeviceEnrollmentReceipt(
            request_id="enroll-1",
            enrollment_id="enrollment-1",
            device_id="device-1",
            lifecycle_state="pending-approval",
            retrieval_expires_at_ms=1,
        ),
    )
    for value in values:
        _validate("onboarding/enrollment.schema.json", value)


def test_handoff_request_and_outcome_conform_to_same_contract() -> None:
    values = (
        DeviceHandoffRequest(
            request_id="handoff-1",
            retrieval_token="device-generated-random-token-000001",
        ),
        DeviceHandoffOutcome(
            request_id="handoff-1",
            enrollment_id="enrollment-1",
            device_id="device-1",
            manifest_revision="sha256:revision",
            lifecycle_state="pending-approval",
        ),
    )
    for value in values:
        _validate("onboarding/handoff.schema.json", value)


def test_golden_enrollment_example_is_accepted() -> None:
    DeviceEnrollment.model_validate_json(
        (EXAMPLES / "device-enrollment.json").read_text(encoding="utf-8")
    )


@settings(max_examples=12, deadline=None)
@given(from_schema(_schema("device/manifest.schema.json")))
def test_manifest_schema_examples_are_accepted_by_wire_model(payload) -> None:
    DeviceManifest.model_validate_json(json.dumps(payload))
