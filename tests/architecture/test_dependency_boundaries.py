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


def test_sdk_dependency_is_confined_to_canonical_contract_adapters() -> None:
    allowed = {
        Path("hub/adapters/security/owner_directory.py"),
        Path("hub/contracts/bindings/onboarding.py"),
        Path("hub/contracts/bindings/device.py"),
    }
    violations = []
    for path in (ROOT / "hub").rglob("*.py"):
        if "eidolon_sdk" in _imports(path) and path.relative_to(ROOT) not in allowed:
            violations.append(str(path.relative_to(ROOT)))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert any(item.split("[", 1)[0].lower() == "eidolon-sdk" for item in dependencies)
    assert "eidolon-sdk" in project.get("tool", {}).get("uv", {}).get("sources", {})
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


def test_hub_has_no_opentelemetry_runtime_or_configuration() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    assert "opentelemetry" not in dependencies
    assert not (ROOT / "hub" / "adapters" / "observability").exists()
    settings = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "config").glob("settings.*.yaml")
    ).lower()
    assert "observability" not in settings
    assert "otel_" not in (ROOT / "config" / ".env.example").read_text().lower()


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
    production_settings = "\n".join(
        path.read_text().lower() for path in (ROOT / "config").glob("settings.*.yaml")
    )
    assert "mqtt" not in production_settings
    assert "connection_plane" not in production_settings


def test_production_config_contains_no_provider_or_hardware_details() -> None:
    config_source = (ROOT / "hub" / "config.py").read_text(encoding="utf-8").lower()
    settings_source = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (ROOT / "config").glob("settings.*.yaml")
    )
    forbidden = {"livekit", "esp32", "room_name", "turn_url", "codec"}

    assert {value for value in forbidden if value in config_source} == set()
    assert {value for value in forbidden if value in settings_source} == set()


def test_configuration_is_local_only_and_has_current_env_contract() -> None:
    assert (ROOT / "config" / "settings.yaml").is_file()
    assert not (ROOT / "config" / "settings.local.yaml").exists()
    assert not (ROOT / "config" / "settings.cloud.yaml").exists()
    assert not (ROOT / "config" / "settings.example.yaml").exists()
    assert not (ROOT / ".env.example").exists()

    environment_source = (ROOT / "config" / ".env.example").read_text(encoding="utf-8")
    required = {
        "EIDOLON_HUB_MANAGEMENT_JWT_SECRET",
        "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN",
        "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
    }
    retired = {
        "EIDOLON_HUB_PROFILE",
        "EIDOLON_HUB_INSTANCE_ID",
        "EIDOLON_HUB_POSTGRES_USER",
        "EIDOLON_HUB_POSTGRES_PASSWORD",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "LIVEKIT_API_URL",
        "MDNS_CONFIG_PATH",
        "EIDOLON_HUB_LEASE_SECRET",
    }

    assert all(f"{name}=" in environment_source for name in required)
    assert all(name not in environment_source for name in retired)


def test_hub_contains_no_cloud_postgresql_or_multi_instance_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    assert "asyncpg" not in dependencies
    retired_source = {
        "PostgresqlPersistenceConfig",
        "DeviceAuthorityLease",
        "DeviceAuthorityRepository",
        "hub_instance_id",
        "fencing_token",
        "EIDOLON_HUB_PROFILE",
        "EIDOLON_HUB_INSTANCE_ID",
    }
    violations = []
    for path in (ROOT / "hub").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        found = sorted(value for value in retired_source if value in source)
        if found:
            violations.append(f"{path.relative_to(ROOT)}: {found}")
    assert violations == []


def test_hub_contains_no_database_migration_or_legacy_schema_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    assert "alembic" not in dependencies
    assert not (ROOT / "hub" / "adapters" / "persistence" / "migrations").exists()
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "hub" / "adapters" / "persistence").rglob("*.py")
    )
    assert "tenant_id" not in sources
    assert "approved: Mapped[bool]" not in sources
    assert "revoked: Mapped[bool]" not in sources


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


def test_hub_does_not_own_provider_channel_lifecycle_or_persistence() -> None:
    retired_source_names = {
        "record_channel_lifecycle.py",
        "provider_gateway.py",
    }
    assert not any(path.name in retired_source_names for path in (ROOT / "hub").rglob("*.py"))
    assert not (
        ROOT / "hub" / "contracts" / "schemas" / "channel" / "lifecycle.schema.json"
    ).exists()

    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "hub").rglob("*.py")
    )
    for retired in (
        "ChannelLeaseRepository",
        "ChannelLifecycle",
        "ChannelState",
        "hub_channel_assignments",
        "/api/provider/v1",
    ):
        assert retired not in sources


def test_hub_has_no_long_lived_device_session_or_presence_runtime() -> None:
    retired_files = {
        "close_session.py",
        "renew_session.py",
        "session_proof.py",
        "directory_worker.py",
    }
    assert not any(path.name in retired_files for path in (ROOT / "hub").rglob("*.py"))
    assert not any((ROOT / "hub" / "domain" / "sessions").rglob("*.py"))
    persistence = (ROOT / "hub" / "adapters" / "persistence" / "models.py").read_text()
    assert "DeviceSessionRow" not in persistence
    assert "ChallengeRow" not in persistence
    settings = (ROOT / "config" / "settings.yaml").read_text().lower()
    for retired in ("session_lease", "heartbeat", "projection_interval"):
        assert retired not in settings


def test_device_directory_does_not_persist_duplicate_projection_or_presence() -> None:
    models = (ROOT / "hub" / "adapters" / "persistence" / "models.py").read_text()
    assert "DirectoryRow" not in models
    schema = (ROOT / "hub" / "contracts" / "schemas" / "device" / "directory.schema.json")
    contents = schema.read_text()
    for forbidden in ("online", "session", "heartbeat"):
        assert forbidden not in contents


def test_hub_contains_no_device_bus_or_channel_payload_runtime() -> None:
    retired_source_names = {
        "send_command.py",
        "get_command.py",
        "ingest_data_envelope.py",
        "data_bridge.py",
    }
    assert not any(path.name in retired_source_names for path in (ROOT / "hub").rglob("*.py"))
    assert not any((ROOT / "hub" / "domain" / "commands").glob("*.py"))
    assert not (ROOT / "hub" / "contracts" / "schemas" / "device" / "command.schema.json").exists()
    assert not (
        ROOT / "hub" / "contracts" / "schemas" / "channel" / "data-envelope.schema.json"
    ).exists()
    persistence = (ROOT / "hub" / "adapters" / "persistence" / "models.py").read_text()
    assert "hub_commands" not in persistence
    assert "hub_channel_cursors" not in persistence


def test_internal_device_management_events_are_not_named_as_a_system_bus() -> None:
    assert not (ROOT / "hub" / "ports" / "event_bus.py").exists()
    assert (ROOT / "hub" / "ports" / "management_events.py").is_file()
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "hub").rglob("*.py"))
    assert "EventBus" not in sources
    assert "SqlEventBus" not in sources


def test_device_facts_and_audit_events_only_use_atomic_mutation_boundary() -> None:
    repository_port = (ROOT / "hub" / "ports" / "repositories.py").read_text()
    persistence = (
        ROOT / "hub" / "adapters" / "persistence" / "repositories.py"
    ).read_text()
    mutation_sources = "\n".join(
        path.read_text()
        for path in (ROOT / "hub" / "application" / "use_cases").glob("*.py")
    )

    assert "class DeviceMutationUnitOfWork" in repository_port
    assert "async def upsert(self, device" not in repository_port
    assert "async def publish(self, event" not in persistence
    assert "_devices.upsert(" not in mutation_sources
    assert "_events.publish(" not in mutation_sources


def test_device_management_query_contract_does_not_add_grpc_tooling() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    assert "grpcio" not in dependencies
    assert "protobuf" not in dependencies
    assert not any((ROOT / "hub").rglob("*.proto"))


def test_generated_build_cache_cannot_reintroduce_deleted_hub_files() -> None:
    """An incremental setuptools build must not silently ship retired files."""

    cached_hub = ROOT / "build" / "lib" / "hub"
    if not cached_hub.exists():
        return
    source_files = {
        path.relative_to(ROOT)
        for path in (ROOT / "hub").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    cached_files = {
        Path("hub") / path.relative_to(cached_hub)
        for path in cached_hub.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert sorted(str(path) for path in cached_files - source_files) == []


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
