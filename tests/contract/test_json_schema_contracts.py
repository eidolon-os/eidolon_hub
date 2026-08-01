from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator

from hub.contracts.bindings.channel import ChannelGrant, ChannelLifecycleEvent, DataEnvelope
from hub.contracts.bindings.connection import ConnectionHello, HubDescriptor
from hub.contracts.bindings.device import (
    DeviceCommandStatus,
    DeviceDirectoryEntry,
    DeviceManifest,
    DeviceRegistration,
)
from hub.contracts.generated.schema_models.channel.data_envelope_schema import (
    DataEnvelope as GeneratedDataEnvelope,
)
from hub.contracts.generated.schema_models.device.manifest_schema import (
    DeviceManifest as GeneratedDeviceManifest,
)
from hub.contracts.guard import parse_guard_message
from hub.contracts.sense import parse_sense_message

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "hub" / "contracts" / "schemas"
EXAMPLES = ROOT / "hub" / "contracts" / "examples"


def _schema(relative: str):
    return json.loads((SCHEMAS / relative).read_text(encoding="utf-8"))


def test_all_contract_schemas_are_valid_draft_2020_12() -> None:
    for path in SCHEMAS.rglob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_generated_contract_shapes_are_current() -> None:
    subprocess.run(
        [sys.executable, "scripts/generate_contracts.py", "--check"],
        cwd=ROOT,
        check=True,
    )


def test_generated_channel_grant_matches_source_schema() -> None:
    grant = ChannelGrant(
        operation_id="channel-sync:sha256:desired",
        channel_id="channel-1",
        purpose="management",
        kinds=("reliable-data",),
        binding_format="application/eidolon-channel+json",
        issued_at_ms=1_799_999_000_000,
        lease_expires_at_ms=1_800_000_000_000,
        opaque_binding="encrypted-provider-binding",
    )

    Draft202012Validator(_schema("channel/channel.schema.json")).validate(
        grant.model_dump(mode="json")
    )


def test_runtime_bindings_conform_to_generated_schema_shapes() -> None:
    manifest = DeviceManifest(title="Generic Device")
    envelope = DataEnvelope(
        envelope_id="envelope-1",
        channel_id="channel-1",
        device_id="device-1",
        kind="state",
        sequence=1,
        occurred_at_ms=1,
        payload_json='{"revision":1,"values_json":"{}"}',
    )

    GeneratedDeviceManifest.model_validate(manifest.model_dump(mode="json"))
    GeneratedDataEnvelope.model_validate(envelope.model_dump(mode="json"))


def test_public_status_bindings_conform_to_schema_sources() -> None:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    values = (
        (
            "connection/descriptor.schema.json",
            HubDescriptor(
                hub_id="hub-1",
                descriptor_uri="https://hub.example/api/connection/v1/descriptor",
                https_registration_uri="https://hub.example/api/connection/v1/register",
                mqtt_endpoint_uri="mqtts://broker.example:8883",
            ),
        ),
        (
            "channel/lifecycle.schema.json",
            ChannelLifecycleEvent(
                channel_id="channel-1",
                device_id="device-1",
                state="active",
                occurred_at_ms=int(now.timestamp() * 1000),
            ),
        ),
        (
            "device/directory.schema.json",
            DeviceDirectoryEntry(
                device_id="device-1",
                owner_scope="owner-1",
                display_name="Device",
                device_kind="generic",
                manifest_json='{"schema_version":1}',
                manifest_revision="sha256:revision",
                approved=True,
                revoked=False,
                online=False,
                registered_at=now,
                updated_at=now,
                revision=1,
            ),
        ),
        (
            "device/status.schema.json",
            DeviceCommandStatus(
                command_id="command-1",
                device_id="device-1",
                command_name="display.render",
                state="sent",
                created_at=now,
                updated_at=now,
                expires_at=now,
            ),
        ),
    )
    for schema_path, model in values:
        Draft202012Validator(_schema(schema_path)).validate(model.model_dump(mode="json"))


def test_golden_contract_examples_are_accepted() -> None:
    ConnectionHello.model_validate_json(
        (EXAMPLES / "connection-hello.json").read_text(encoding="utf-8")
    )
    DeviceRegistration.model_validate_json(
        (EXAMPLES / "device-registration.json").read_text(encoding="utf-8")
    )
    ChannelGrant.model_validate_json((EXAMPLES / "channel-grant.json").read_text(encoding="utf-8"))
    parse_sense_message(json.loads((EXAMPLES / "sense-attention.json").read_text()))
    parse_guard_message(json.loads((EXAMPLES / "guard-candidate.json").read_text()))


def test_guard_and_sense_examples_conform_to_schema_sources() -> None:
    Draft202012Validator(_schema("sense/message.schema.json")).validate(
        json.loads((EXAMPLES / "sense-attention.json").read_text())
    )
    Draft202012Validator(_schema("guard/message.schema.json")).validate(
        json.loads((EXAMPLES / "guard-candidate.json").read_text())
    )


@settings(max_examples=12, deadline=None)
@given(from_schema(_schema("device/manifest.schema.json")))
def test_manifest_schema_examples_are_accepted_by_wire_model(payload) -> None:
    DeviceManifest.model_validate_json(json.dumps(payload))
