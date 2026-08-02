from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_CORE_ROOTS = {
    "fastapi",
    "nats",
    "zeroconf",
    "aiomqtt",
    "livekit",
    "eidolon_data",
    "eidolon_sdk",
}


def _imports(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_domain_and_application_do_not_import_infrastructure() -> None:
    violations = []
    for layer in (ROOT / "hub" / "domain", ROOT / "hub" / "application"):
        for path in layer.rglob("*.py"):
            forbidden = _imports(path) & FORBIDDEN_CORE_ROOTS
            if forbidden:
                violations.append(f"{path.relative_to(ROOT)}: {sorted(forbidden)}")
    assert violations == []


def test_hub_has_no_direct_sdk_source_or_project_dependency() -> None:
    violations = []
    for source_root in (ROOT / "hub", ROOT / "tests", ROOT / "scripts"):
        for path in source_root.rglob("*.py"):
            if "eidolon_sdk" in _imports(path):
                violations.append(str(path.relative_to(ROOT)))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert not any(item.split("[", 1)[0].lower() == "eidolon-sdk" for item in dependencies)
    assert "eidolon-sdk" not in project.get("tool", {}).get("uv", {}).get("sources", {})
    assert violations == []


def test_hub_has_no_livekit_dependency_or_source_import() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        item.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].lower()
        for item in project["project"]["dependencies"]
    }
    dependencies.update(
        item.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].lower()
        for item in project.get("dependency-groups", {}).get("dev", [])
    )
    assert "livekit" not in dependencies
    assert "livekit-api" not in dependencies
    assert not any("livekit" in _imports(path) for path in (ROOT / "hub").rglob("*.py"))


def test_hub_does_not_import_eidolon_channel() -> None:
    violations = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "hub").rglob("*.py")
        if "eidolon_channel" in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_hub_has_no_nats_or_eidolon_data_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    sources = project.get("tool", {}).get("uv", {}).get("sources", {})
    violations = []
    for path in (ROOT / "hub").rglob("*.py"):
        forbidden = _imports(path) & {"nats", "eidolon_data"}
        if forbidden:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(forbidden)}")

    assert "nats-py" not in dependencies
    assert "eidolon-data" not in dependencies
    assert "eidolon-data" not in sources
    assert violations == []


def test_production_configuration_has_no_nats_setting() -> None:
    paths = [ROOT / "hub" / "config.py", *sorted((ROOT / "config").glob("*.yaml"))]
    violations = [
        str(path.relative_to(ROOT)) for path in paths if "nats" in path.read_text().lower()
    ]
    assert violations == []


def test_sqlalchemy_is_confined_to_persistence_and_composition() -> None:
    allowed_roots = {
        Path("hub/adapters/persistence"),
        Path("hub/composition"),
    }
    violations = []
    for path in (ROOT / "hub").rglob("*.py"):
        if "sqlalchemy" not in _imports(path):
            continue
        relative = path.relative_to(ROOT)
        if not any(relative.is_relative_to(root) for root in allowed_roots):
            violations.append(str(relative))
    assert violations == []


def test_production_entry_and_interfaces_do_not_use_legacy_service_locator() -> None:
    production_sources = [ROOT / "hub" / "main.py"]
    production_sources.extend((ROOT / "hub" / "interfaces").rglob("*.py"))
    violations = []
    for path in production_sources:
        source = path.read_text(encoding="utf-8")
        forbidden_imports = _imports(path) & {"eidolon_data", "livekit", "eidolon_sdk"}
        if forbidden_imports or "app.state.data_store" in source:
            violations.append(f"{path.relative_to(ROOT)}: imports={sorted(forbidden_imports)}")
    assert violations == []


def test_hub_has_no_transport_connector_or_mqtt_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    assert "aiomqtt" not in dependencies
    assert not (ROOT / "hub" / "ports" / "connections.py").exists()
    assert not any((ROOT / "hub" / "adapters" / "connections").glob("*.py"))
    assert not any((ROOT / "hub" / "domain" / "connections").glob("*.py"))
    production_settings = (ROOT / "config" / "settings.yaml").read_text().lower()
    assert "mqtt" not in production_settings
    assert "connection_plane" not in production_settings


def test_production_config_contains_no_provider_or_hardware_details() -> None:
    config_source = (ROOT / "hub" / "config.py").read_text(encoding="utf-8").lower()
    settings_source = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8").lower()
    forbidden = {"livekit", "esp32", "room_name", "turn_url", "codec"}

    assert {value for value in forbidden if value in config_source} == set()
    assert {value for value in forbidden if value in settings_source} == set()


def test_configuration_has_one_canonical_yaml_and_current_env_contract() -> None:
    assert (ROOT / "config" / "settings.yaml").is_file()
    assert not (ROOT / "config" / "settings.example.yaml").exists()
    assert not (ROOT / ".env.example").exists()

    environment_source = (ROOT / "config" / ".env.example").read_text(encoding="utf-8")
    required = {
        "EIDOLON_HUB_LEASE_SECRET",
        "EIDOLON_HUB_MANAGEMENT_JWT_SECRET",
        "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
    }
    retired = {"LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_API_URL", "MDNS_CONFIG_PATH"}

    assert all(f"{name}=" in environment_source for name in required)
    assert all(name not in environment_source for name in retired)


def test_hub_has_no_legacy_channel_profile_or_device_negotiation_source() -> None:
    retired = {
        "ChannelProfile",
        "provisioner_ref",
        "profile_name",
        "channel.offer",
        "channel.accept",
    }
    violations = []
    for path in (ROOT / "hub").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        found = sorted(value for value in retired if value in source)
        if found:
            violations.append(f"{path.relative_to(ROOT)}: {found}")
    assert violations == []


def test_opaque_channel_bindings_cannot_be_persisted() -> None:
    persistence_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "hub" / "adapters" / "persistence").rglob("*.py")
    )
    assert "opaque_binding" not in persistence_sources


def test_legacy_runtime_has_been_removed() -> None:
    assert not any((ROOT / "hub" / "legacy").rglob("*.py"))


def test_domain_application_and_ports_only_use_standard_library_and_hub_types() -> None:
    violations = []
    for layer in (
        ROOT / "hub" / "domain",
        ROOT / "hub" / "application",
        ROOT / "hub" / "ports",
    ):
        for path in layer.rglob("*.py"):
            third_party = {
                root
                for root in _imports(path)
                if root != "hub" and root not in sys.stdlib_module_names
            }
            if third_party:
                violations.append(f"{path.relative_to(ROOT)}: {sorted(third_party)}")
    assert violations == []
