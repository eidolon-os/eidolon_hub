"""Hub unified config loader — reads exclusively from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bootstrap_dotenv() -> None:
    from dotenv import load_dotenv

    load_dotenv()


_bootstrap_dotenv()


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8081

    @classmethod
    def from_env(cls) -> "ApiConfig":
        return cls(
            host=os.environ.get("HUB_HOST", "0.0.0.0"),
            port=int(os.environ.get("HUB_PORT", "8081")),
        )


@dataclass
class LoggingConfig:
    level: str = "INFO"

    @classmethod
    def from_env(cls) -> "LoggingConfig":
        return cls(level=os.environ.get("LOG_LEVEL", "INFO"))


@dataclass
class LiveKitConfig:
    url: str = ""
    api_key: str = ""
    api_secret: str = ""

    @classmethod
    def from_env(cls) -> "LiveKitConfig":
        explicit_url = os.environ.get("LIVEKIT_URL", "")
        fallback_url = os.environ.get("EIDOLON_LIVEKIT_URL", "")
        return cls(
            url=explicit_url or fallback_url,
            api_key=os.environ.get("LIVEKIT_API_KEY", ""),
            api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
        )


@dataclass
class Esp32Config:
    server_url: str = ""

    @classmethod
    def from_env(cls) -> "Esp32Config":
        return cls(
            server_url=os.environ.get("EIDOLON_LIVEKIT_URL", ""),
        )


@dataclass
class DiscoveryConfig:
    service_type: str = "_eidolon-hub._tcp.local."
    service_name: str = ""
    hostname: str = "eidolon-hub"
    txt_version: str = "1"
    api_version: str = "v1"
    config_path: str = "/api/config"

    @classmethod
    def from_env(cls) -> "DiscoveryConfig":
        return cls(
            service_type=os.environ.get("MDNS_SERVICE_TYPE", "_eidolon-hub._tcp.local."),
            service_name=os.environ.get("MDNS_SERVICE_NAME", ""),
            hostname=os.environ.get("MDNS_HOSTNAME", "eidolon-hub"),
            txt_version=os.environ.get("MDNS_TXT_VERSION", "1"),
            api_version=os.environ.get("MDNS_API_VERSION", "v1"),
            config_path=os.environ.get("MDNS_CONFIG_PATH", "/api/config"),
        )


@dataclass
class AdminConfig:
    probe_enabled: bool = True
    probe_interval_seconds: int = 10
    offline_after_missed_probes: int = 3
    degraded_after_missed_probes: int = 2
    command_timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "AdminConfig":
        return cls(
            probe_enabled=os.environ.get("ADMIN_PROBE_ENABLED", "true").lower() in {"1", "true", "yes"},
            probe_interval_seconds=int(os.environ.get("ADMIN_PROBE_INTERVAL_SECONDS", "10")),
            offline_after_missed_probes=int(os.environ.get("ADMIN_OFFLINE_AFTER_MISSED_PROBES", "3")),
            degraded_after_missed_probes=int(os.environ.get("ADMIN_DEGRADED_AFTER_MISSED_PROBES", "2")),
            command_timeout_seconds=int(os.environ.get("ADMIN_COMMAND_TIMEOUT_SECONDS", "30")),
        )


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    livekit: LiveKitConfig = field(default_factory=LiveKitConfig)
    esp32: Esp32Config = field(default_factory=Esp32Config)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            api=ApiConfig.from_env(),
            logging=LoggingConfig.from_env(),
            livekit=LiveKitConfig.from_env(),
            esp32=Esp32Config.from_env(),
            discovery=DiscoveryConfig.from_env(),
            admin=AdminConfig.from_env(),
        )


def load_config() -> AppConfig:
    return AppConfig.load()
