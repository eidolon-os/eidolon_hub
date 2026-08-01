"""Production configuration for the protocol-neutral Hub control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_YAML = _REPO_ROOT / "config" / "settings.yaml"
_DEFAULT_ENV = _REPO_ROOT / "config" / ".env"


def _resolve_settings_yaml() -> Path:
    explicit = os.environ.get("EIDOLON_HUB_SETTINGS_YAML", "").strip()
    path = Path(explicit).expanduser() if explicit else _DEFAULT_YAML
    if not path.is_file():
        raise FileNotFoundError(f"Hub settings file is missing: {path}")
    return path.resolve()


def _resolve_env_file() -> Path:
    explicit = os.environ.get("EIDOLON_HUB_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    else:
        path = _DEFAULT_ENV
    if not path.is_file():
        raise FileNotFoundError(f"Hub environment file is missing: {path}")
    return path.resolve()


def _bootstrap_dotenv() -> None:
    from dotenv import load_dotenv

    load_dotenv(_resolve_env_file(), override=False)


def _load_yaml() -> dict[str, Any]:
    value = yaml.safe_load(_resolve_settings_yaml().read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("Hub settings must be a YAML object")
    return value


def _section(value: dict[str, Any], key: str) -> dict[str, Any]:
    section = value.get(key) or {}
    if not isinstance(section, dict):
        raise ValueError(f"{key} must be a YAML object")
    return section


def _reject_unknown(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown Hub settings at {path}: {', '.join(unknown)}")


def _boolean(value: dict[str, Any], key: str, default: bool, path: str) -> bool:
    raw = value.get(key, default)
    if not isinstance(raw, bool):
        raise ValueError(f"{path}.{key} must be a YAML boolean")
    return raw


def _is_loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_settings_shape(value: dict[str, Any]) -> None:
    """Fail closed when retired or misspelled settings remain in YAML."""

    _reject_unknown(
        value,
        {"api", "observability", "connection_plane", "channel_provider", "persistence"},
        "root",
    )
    _reject_unknown(_section(value, "api"), {"host", "port"}, "api")
    _reject_unknown(
        _section(value, "observability"),
        {"enabled", "service_name", "otlp_endpoint"},
        "observability",
    )
    connection = _section(value, "connection_plane")
    _reject_unknown(
        connection,
        {
            "hub_id",
            "hub_instance_id",
            "public_base_url",
            "lease_seconds",
            "heartbeat_after_ms",
            "mdns",
            "mqtt",
        },
        "connection_plane",
    )
    _reject_unknown(
        _section(connection, "mdns"),
        {"enabled", "service_type", "service_name", "hostname"},
        "connection_plane.mdns",
    )
    _reject_unknown(
        _section(connection, "mqtt"),
        {"enabled", "connector_id", "hostname", "port", "username", "password_env", "priority"},
        "connection_plane.mqtt",
    )
    _reject_unknown(
        _section(value, "channel_provider"),
        {"contract_url"},
        "channel_provider",
    )
    _reject_unknown(
        _section(value, "persistence"),
        {
            "adapter",
            "sqlite_path",
            "postgresql_dsn_env",
            "init_schema",
            "pool_size",
            "max_overflow",
            "directory_cache_enabled",
            "reconciliation_seconds",
        },
        "persistence",
    )


@dataclass(frozen=True, slots=True)
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8082


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    enabled: bool = True
    service_name: str = "eidolon-hub"
    otlp_endpoint: str = ""


@dataclass(frozen=True, slots=True)
class MdnsDiscoveryConfig:
    enabled: bool = True
    service_type: str = "_eidolon-hub._tcp.local."
    service_name: str = ""
    hostname: str = "eidolon-hub"


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    adapter: str = "sqlite"
    sqlite_path: str = "var/eidolon-hub.sqlite3"
    postgresql_dsn_env: str = "EIDOLON_HUB_POSTGRES_DSN"
    init_schema: bool = True
    pool_size: int = 10
    max_overflow: int = 20
    directory_cache_enabled: bool = True
    reconciliation_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class MqttConnectorConfig:
    enabled: bool = False
    connector_id: str = "mqtt-cloud"
    hostname: str = ""
    port: int = 8883
    username: str = ""
    password_env: str = "EIDOLON_HUB_MQTT_PASSWORD"
    priority: int = 100


@dataclass(frozen=True, slots=True)
class ConnectionPlaneConfig:
    hub_id: str = "eidolon-hub-local"
    hub_instance_id: str = "eidolon-hub-local-1"
    public_base_url: str = "https://eidolon-hub.local:8082"
    lease_seconds: int = 45
    heartbeat_after_ms: int = 15_000
    mdns: MdnsDiscoveryConfig = field(default_factory=MdnsDiscoveryConfig)
    mqtt: MqttConnectorConfig = field(default_factory=MqttConnectorConfig)


@dataclass(frozen=True, slots=True)
class ChannelProviderConfig:
    contract_url: str = "http://127.0.0.1:8090/v1"


@dataclass(frozen=True, slots=True)
class HubConfig:
    """Only deployment settings understood by the production Hub."""

    api: ApiConfig = field(default_factory=ApiConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    connection_plane: ConnectionPlaneConfig = field(default_factory=ConnectionPlaneConfig)
    channel_provider: ChannelProviderConfig = field(default_factory=ChannelProviderConfig)

    @classmethod
    def load(cls) -> HubConfig:
        _bootstrap_dotenv()
        source = _load_yaml()
        _validate_settings_shape(source)
        config = cls(
            api=_api_from_yaml(source),
            observability=_observability_from_yaml(source),
            persistence=_persistence_from_yaml(source),
            connection_plane=_connection_plane_from_yaml(source),
            channel_provider=_channel_provider_from_yaml(source),
        )
        validate_hub_config(config)
        return config


def _api_from_yaml(value: dict[str, Any]) -> ApiConfig:
    section = _section(value, "api")
    return ApiConfig(
        host=str(section.get("host") or "0.0.0.0"), port=int(section.get("port", 8082))
    )


def _observability_from_yaml(value: dict[str, Any]) -> ObservabilityConfig:
    section = _section(value, "observability")
    endpoint = (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or str(section.get("otlp_endpoint") or "")
    ).strip()
    return ObservabilityConfig(
        enabled=_boolean(section, "enabled", True, "observability"),
        service_name=str(section.get("service_name") or "eidolon-hub").strip(),
        otlp_endpoint=endpoint,
    )


def _mdns_from_yaml(value: dict[str, Any]) -> MdnsDiscoveryConfig:
    section = _section(value, "mdns")
    return MdnsDiscoveryConfig(
        enabled=_boolean(section, "enabled", True, "connection_plane.mdns"),
        service_type=str(section.get("service_type") or "_eidolon-hub._tcp.local."),
        service_name=str(section.get("service_name") or ""),
        hostname=str(section.get("hostname") or "eidolon-hub"),
    )


def _persistence_from_yaml(value: dict[str, Any]) -> PersistenceConfig:
    section = _section(value, "persistence")
    return PersistenceConfig(
        adapter=str(section.get("adapter") or "sqlite").strip().lower(),
        sqlite_path=str(section.get("sqlite_path") or "var/eidolon-hub.sqlite3").strip(),
        postgresql_dsn_env=str(
            section.get("postgresql_dsn_env") or "EIDOLON_HUB_POSTGRES_DSN"
        ).strip(),
        init_schema=_boolean(section, "init_schema", True, "persistence"),
        pool_size=int(section.get("pool_size", 10)),
        max_overflow=int(section.get("max_overflow", 20)),
        directory_cache_enabled=_boolean(section, "directory_cache_enabled", True, "persistence"),
        reconciliation_seconds=float(section.get("reconciliation_seconds", 5.0)),
    )


def _connection_plane_from_yaml(value: dict[str, Any]) -> ConnectionPlaneConfig:
    section = _section(value, "connection_plane")
    mqtt = _section(section, "mqtt")
    return ConnectionPlaneConfig(
        hub_id=str(section.get("hub_id") or "eidolon-hub-local"),
        hub_instance_id=str(section.get("hub_instance_id") or "eidolon-hub-local-1"),
        public_base_url=str(
            section.get("public_base_url") or "https://eidolon-hub.local:8082"
        ).rstrip("/"),
        lease_seconds=int(section.get("lease_seconds", 45)),
        heartbeat_after_ms=int(section.get("heartbeat_after_ms", 15_000)),
        mdns=_mdns_from_yaml(section),
        mqtt=MqttConnectorConfig(
            enabled=_boolean(mqtt, "enabled", False, "connection_plane.mqtt"),
            connector_id=str(mqtt.get("connector_id") or "mqtt-cloud"),
            hostname=str(mqtt.get("hostname") or ""),
            port=int(mqtt.get("port", 8883)),
            username=str(mqtt.get("username") or ""),
            password_env=str(mqtt.get("password_env") or "EIDOLON_HUB_MQTT_PASSWORD"),
            priority=int(mqtt.get("priority", 100)),
        ),
    )


def _channel_provider_from_yaml(value: dict[str, Any]) -> ChannelProviderConfig:
    section = _section(value, "channel_provider")
    return ChannelProviderConfig(
        contract_url=str(section.get("contract_url") or "http://127.0.0.1:8090/v1").rstrip("/")
    )


def validate_hub_config(config: HubConfig) -> None:
    if not config.api.host.strip():
        raise ValueError("api.host is required")
    if not 1 <= config.api.port <= 65_535:
        raise ValueError("api.port must be between 1 and 65535")
    if (
        not config.connection_plane.hub_id.strip()
        or not config.connection_plane.hub_instance_id.strip()
    ):
        raise ValueError("connection_plane hub identifiers are required")
    if not config.connection_plane.public_base_url.startswith("https://"):
        raise ValueError("connection_plane.public_base_url must use HTTPS")
    if config.connection_plane.lease_seconds < 15:
        raise ValueError("connection_plane.lease_seconds must be at least 15")
    if not 1_000 <= config.connection_plane.heartbeat_after_ms <= 300_000:
        raise ValueError("connection_plane.heartbeat_after_ms is out of range")
    mdns = config.connection_plane.mdns
    if mdns.enabled:
        if not mdns.service_type.startswith("_") or not mdns.service_type.endswith(".local."):
            raise ValueError("mDNS service_type must be a .local. service type")
        if mdns.service_name and not mdns.service_name.endswith(mdns.service_type):
            raise ValueError("mDNS service_name must belong to service_type")
        if not mdns.hostname.strip():
            raise ValueError("enabled mDNS discovery requires hostname")
    mqtt = config.connection_plane.mqtt
    if mqtt.enabled and not mqtt.hostname:
        raise ValueError("enabled MQTT connector requires hostname")
    if not 1 <= mqtt.port <= 65_535:
        raise ValueError("MQTT port must be between 1 and 65535")
    persistence = config.persistence
    if persistence.adapter not in {"sqlite", "postgresql"}:
        raise ValueError("persistence.adapter must be sqlite or postgresql")
    if persistence.adapter == "sqlite" and not persistence.sqlite_path:
        raise ValueError("SQLite persistence requires sqlite_path")
    if persistence.adapter == "postgresql" and not persistence.postgresql_dsn_env:
        raise ValueError("PostgreSQL persistence requires postgresql_dsn_env")
    if persistence.pool_size < 1 or persistence.max_overflow < 0:
        raise ValueError("invalid persistence pool sizing")
    if persistence.reconciliation_seconds <= 0:
        raise ValueError("persistence.reconciliation_seconds must be positive")
    provider_url = urlparse(config.channel_provider.contract_url)
    if (
        provider_url.scheme not in {"http", "https"}
        or not provider_url.netloc
        or provider_url.username is not None
        or provider_url.password is not None
        or provider_url.query
        or provider_url.fragment
    ):
        raise ValueError("channel_provider.contract_url must be a plain HTTP(S) base URL")
    if provider_url.scheme == "http" and not _is_loopback_hostname(provider_url.hostname):
        raise ValueError("remote Channel Provider must use HTTPS")


def load_hub_config() -> HubConfig:
    return HubConfig.load()
