"""Production configuration for the protocol-neutral Hub control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_YAML = _REPO_ROOT / "config" / "settings.yaml"
_DEFAULT_ENV = _REPO_ROOT / "config" / ".env"
_ROOT_ENV = _REPO_ROOT / ".env"


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
    elif _DEFAULT_ENV.is_file():
        path = _DEFAULT_ENV
    else:
        path = _ROOT_ENV
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


@dataclass(frozen=True, slots=True)
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8082


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    enabled: bool = True
    service_name: str = "eidolon-hub"
    otlp_endpoint: str = ""


@dataclass(frozen=True, slots=True)
class MdnsConfig:
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
    mqtt: MqttConnectorConfig = field(default_factory=MqttConnectorConfig)
    unicast_dns_sd_service: str = ""
    explicit_descriptor_uris: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChannelProfileConfig:
    required_kinds: tuple[str, ...]
    provisioner_ref: str


@dataclass(frozen=True, slots=True)
class ChannelControlConfig:
    profiles: dict[str, ChannelProfileConfig] = field(
        default_factory=lambda: {
            "management-data": ChannelProfileConfig(
                required_kinds=("reliable-data",),
                provisioner_ref="channel-provisioner/default-data",
            ),
            "realtime-media": ChannelProfileConfig(
                required_kinds=("realtime-data", "audio", "video"),
                provisioner_ref="channel-provisioner/realtime",
            ),
        }
    )
    provider_endpoints: dict[str, str] = field(
        default_factory=lambda: {
            "channel-provisioner/default-data": "http://127.0.0.1:8090",
            "channel-provisioner/realtime": "http://127.0.0.1:8090",
        }
    )
    provider_token_env: str = "EIDOLON_HUB_PROVIDER_TOKEN"
    management_profile: str = "management-data"


@dataclass(frozen=True, slots=True)
class HubConfig:
    """Only deployment settings understood by the production Hub."""

    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    mdns: MdnsConfig = field(default_factory=MdnsConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    connection_plane: ConnectionPlaneConfig = field(default_factory=ConnectionPlaneConfig)
    channel_control: ChannelControlConfig = field(default_factory=ChannelControlConfig)

    @classmethod
    def load(cls) -> HubConfig:
        _bootstrap_dotenv()
        source = _load_yaml()
        config = cls(
            api=_api_from_yaml(source),
            logging=_logging_from_yaml(source),
            observability=_observability_from_yaml(source),
            mdns=_mdns_from_yaml(source),
            persistence=_persistence_from_yaml(source),
            connection_plane=_connection_plane_from_yaml(source),
            channel_control=_channel_control_from_yaml(source),
        )
        validate_hub_config(config)
        return config


def _api_from_yaml(value: dict[str, Any]) -> ApiConfig:
    section = _section(value, "api")
    return ApiConfig(
        host=str(section.get("host") or "0.0.0.0"), port=int(section.get("port", 8082))
    )


def _logging_from_yaml(value: dict[str, Any]) -> LoggingConfig:
    section = _section(value, "logging")
    return LoggingConfig(level=str(section.get("level") or "INFO").upper())


def _observability_from_yaml(value: dict[str, Any]) -> ObservabilityConfig:
    section = _section(value, "observability")
    endpoint = (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or str(section.get("otlp_endpoint") or "")
    ).strip()
    return ObservabilityConfig(
        enabled=bool(section.get("enabled", True)),
        service_name=str(section.get("service_name") or "eidolon-hub").strip(),
        otlp_endpoint=endpoint,
    )


def _mdns_from_yaml(value: dict[str, Any]) -> MdnsConfig:
    section = _section(value, "mdns")
    return MdnsConfig(
        enabled=bool(section.get("enabled", True)),
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
        init_schema=bool(section.get("init_schema", True)),
        pool_size=int(section.get("pool_size", 10)),
        max_overflow=int(section.get("max_overflow", 20)),
        directory_cache_enabled=bool(section.get("directory_cache_enabled", True)),
        reconciliation_seconds=float(section.get("reconciliation_seconds", 5.0)),
    )


def _connection_plane_from_yaml(value: dict[str, Any]) -> ConnectionPlaneConfig:
    section = _section(value, "connection_plane")
    mqtt = _section(section, "mqtt")
    explicit = section.get("explicit_descriptor_uris") or []
    if not isinstance(explicit, list):
        raise ValueError("connection_plane.explicit_descriptor_uris must be a list")
    return ConnectionPlaneConfig(
        hub_id=str(section.get("hub_id") or "eidolon-hub-local"),
        hub_instance_id=str(section.get("hub_instance_id") or "eidolon-hub-local-1"),
        public_base_url=str(
            section.get("public_base_url") or "https://eidolon-hub.local:8082"
        ).rstrip("/"),
        lease_seconds=int(section.get("lease_seconds", 45)),
        heartbeat_after_ms=int(section.get("heartbeat_after_ms", 15_000)),
        mqtt=MqttConnectorConfig(
            enabled=bool(mqtt.get("enabled", False)),
            connector_id=str(mqtt.get("connector_id") or "mqtt-cloud"),
            hostname=str(mqtt.get("hostname") or ""),
            port=int(mqtt.get("port", 8883)),
            username=str(mqtt.get("username") or ""),
            password_env=str(mqtt.get("password_env") or "EIDOLON_HUB_MQTT_PASSWORD"),
            priority=int(mqtt.get("priority", 100)),
        ),
        unicast_dns_sd_service=str(section.get("unicast_dns_sd_service") or ""),
        explicit_descriptor_uris=tuple(str(item) for item in explicit),
    )


def _channel_control_from_yaml(value: dict[str, Any]) -> ChannelControlConfig:
    section = _section(value, "channel_control")
    raw_profiles = _section(section, "profiles")
    profiles: dict[str, ChannelProfileConfig] = {}
    for name, item in raw_profiles.items():
        if not isinstance(item, dict):
            raise ValueError(f"channel_control.profiles.{name} must be an object")
        raw_kinds = item.get("required_kinds") or []
        if not isinstance(raw_kinds, list):
            raise ValueError(f"channel_control.profiles.{name}.required_kinds must be a list")
        profiles[str(name)] = ChannelProfileConfig(
            required_kinds=tuple(str(kind) for kind in raw_kinds),
            provisioner_ref=str(item.get("provisioner_ref") or ""),
        )
    raw_endpoints = _section(section, "provider_endpoints")
    return ChannelControlConfig(
        profiles=profiles,
        provider_endpoints={str(key): str(item).rstrip("/") for key, item in raw_endpoints.items()},
        provider_token_env=str(section.get("provider_token_env") or "EIDOLON_HUB_PROVIDER_TOKEN"),
        management_profile=str(section.get("management_profile") or "management-data"),
    )


def validate_hub_config(config: HubConfig) -> None:
    if not 1 <= config.api.port <= 65_535:
        raise ValueError("api.port must be between 1 and 65535")
    if not config.connection_plane.public_base_url.startswith("https://"):
        raise ValueError("connection_plane.public_base_url must use HTTPS")
    if config.connection_plane.lease_seconds < 15:
        raise ValueError("connection_plane.lease_seconds must be at least 15")
    if not 1_000 <= config.connection_plane.heartbeat_after_ms <= 300_000:
        raise ValueError("connection_plane.heartbeat_after_ms is out of range")
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
    channels = config.channel_control
    if not channels.profiles:
        raise ValueError("at least one channel profile is required")
    if channels.management_profile not in channels.profiles:
        raise ValueError("channel_control.management_profile must name a configured profile")
    for name, profile in channels.profiles.items():
        if not profile.required_kinds:
            raise ValueError(f"channel profile {name} has no required kinds")
        if profile.provisioner_ref not in channels.provider_endpoints:
            raise ValueError(f"channel profile {name} references an unknown provisioner")
        endpoint = channels.provider_endpoints[profile.provisioner_ref]
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(f"channel profile {name} has an invalid Provider endpoint")
    if not channels.provider_token_env.strip():
        raise ValueError("channel_control.provider_token_env is required")


def load_hub_config() -> HubConfig:
    return HubConfig.load()
