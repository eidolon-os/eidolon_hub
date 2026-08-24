from __future__ import annotations

import ast
import json
import re
from pathlib import Path


def test_canonical_admission_has_no_channel_kernel_companion_or_delivery_dependency() -> None:
    root = Path(__file__).resolve().parents[2] / "hub" / "admission"
    forbidden = (
        "hub.domain.channels",
        "hub.ports.channels",
        "channel",
        "kernel",
        "companion",
        "delivery",
    )
    violations: list[str] = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                if any(token in module.lower() for token in forbidden):
                    violations.append(f"{path.name}:{node.lineno}:{module}")
    assert violations == []


def test_canonical_admission_router_is_not_wired_to_default_pi5_traffic() -> None:
    composition = (
        Path(__file__).resolve().parents[2] / "hub" / "composition" / "app.py"
    ).read_text(encoding="utf-8")
    assert "create_admission_router" not in composition


def test_canonical_admission_target_exports_only_sdk_owned_models() -> None:
    root = Path(__file__).resolve().parents[2]
    bindings = root / "hub" / "contracts" / "bindings" / "admission.py"
    tree = ast.parse(bindings.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {"eidolon_sdk.device_foundation.v1"}

    target = (root / "hub" / "admission" / "target_app.py").read_text(encoding="utf-8")
    assert "create_admission_router" in target
    assert "device_onboarding" not in target
    assert "device_management" not in target


def test_canonical_admission_uses_only_frozen_source_and_event_catalog() -> None:
    workspace = Path(__file__).resolve().parents[3]
    source = (workspace / "eidolon_hub" / "hub" / "admission" / "application.py").read_text(
        encoding="utf-8"
    )
    catalog = json.loads(
        (
            workspace
            / "eidolon_sdk"
            / "contracts"
            / "device_foundation"
            / "v1"
            / "events"
            / "schemas.schema.json"
        ).read_text(encoding="utf-8")
    )["$defs"]["AdmissionEventType"]["enum"]
    emitted = set(re.findall(r"live\.eidolon\.device\.[a-z-]+\.v1", source))
    assert emitted
    assert emitted <= set(catalog)
    assert 'SOURCE = "urn:eidolon:authority:admission"' in source
    assert "StateChanged" not in source
    assert "EpochChanged" not in source


def test_canonical_admission_uses_only_frozen_problem_codes_and_never_maps_to_503() -> None:
    workspace = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (
            workspace
            / "eidolon_sdk"
            / "contracts"
            / "device_foundation"
            / "v1"
            / "common"
            / "schemas.schema.json"
        ).read_text(encoding="utf-8")
    )
    allowed = set(schema["$defs"]["DeviceProblem"]["properties"]["code"]["enum"])
    root = workspace / "eidolon_hub" / "hub" / "admission"
    emitted: set[str] = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "AdmissionProblem":
                code = node.args[0]
                if isinstance(code, ast.Constant) and isinstance(code.value, str):
                    emitted.add(code.value)
    assert emitted
    assert emitted <= allowed
    http_source = (root / "http.py").read_text(encoding="utf-8")
    assert "status=503" not in http_source


def test_canonical_admission_contains_no_legacy_synchronous_callsite() -> None:
    root = Path(__file__).resolve().parents[2] / "hub" / "admission"
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))
    for forbidden in (
        "ProvisionDeviceChannels",
        "ChannelProvider",
        "KernelMount",
        "BodyAssignment",
        "CompanionAssignment",
    ):
        assert forbidden not in text


def test_ph2b_cutover_manifest_names_real_callsites_and_keeps_switch_closed() -> None:
    workspace = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (
            workspace / "eidolon_hub" / "hub" / "admission" / "ph2b_consumer_cutover.v1.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["contract_sdk_commit"] == ("d88196757e8c054befd2c17f7cb9c7a9eb6f5253")
    assert manifest["default_writer_switch_authorized"] is False
    assert manifest["migration"] == {
        "mode": "additive_physical_bridge",
        "domain_compatibility": "none",
        "legacy_table_backfill": False,
        "default_writer_activated": False,
        "tables": [
            "admission_proposals_v1",
            "admission_decisions_v1",
            "admission_claim_grants_v1",
            "admission_grant_acks_v1",
            "admission_claims_v1",
            "admission_command_results_v1",
            "admission_outbox_v1",
            "admission_claim_event_stream_v1",
        ],
        "forward_additive_columns": [
            "admission_claim_grants_v1.wire_envelope_json",
        ],
        "isolated_physical_legacy": [
            "admission_claim_grants_v1.sealed_grant is never read or written by the canonical target and is removed only after coordinated consumer activation",
        ],
    }
    expected_owners = {
        "eidolon_admin",
        "eidolon_client_mobile",
        "eidolon_kernel",
        "eidolon-client-esp32",
        "eidolon_channel",
        "eidolon_hub_legacy_and_projections",
    }
    assert {entry["owner"] for entry in manifest["consumers"]} == expected_owners
    for entry in manifest["consumers"]:
        repository = (
            "eidolon_hub"
            if entry["owner"] == "eidolon_hub_legacy_and_projections"
            else entry["owner"]
        )
        for callsite in entry["callsites"]:
            assert (workspace / repository / callsite).is_file(), (
                entry["owner"],
                callsite,
            )
        assert entry["delete"]
        assert entry["target_tests"]


def test_every_hub_ph2a_requirement_maps_to_a_real_test_and_frozen_sdk_requirement() -> None:
    workspace = Path(__file__).resolve().parents[3]
    mapping = json.loads(
        (
            workspace / "eidolon_hub" / "hub" / "admission" / "requirement_test_mapping.v1.json"
        ).read_text(encoding="utf-8")
    )
    sdk_requirements = json.loads(
        (
            workspace
            / "eidolon_sdk"
            / "contracts"
            / "device_foundation"
            / "v1"
            / "requirements"
            / "requirements.json"
        ).read_text(encoding="utf-8")
    )
    frozen_ids = {entry["requirement_id"] for entry in sdk_requirements["requirements"]}
    test_names: set[str] = set()
    for path in (workspace / "eidolon_hub" / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        test_names.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    assert mapping["contract_sdk_commit"] == ("d88196757e8c054befd2c17f7cb9c7a9eb6f5253")
    assert mapping["requirements"]
    for requirement in mapping["requirements"]:
        assert requirement["requirement_id"] in frozen_ids
        assert requirement["tests"]
        assert set(requirement["tests"]) <= test_names


def test_canonical_target_never_reads_or_writes_legacy_opaque_grant() -> None:
    root = Path(__file__).resolve().parents[2] / "hub" / "admission"
    canonical = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))
    assert "sealed_grant" not in canonical
