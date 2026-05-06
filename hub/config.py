"""Hub unified config loader.

Priority: .env env vars > YAML config file > code defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bootstrap_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_bootstrap_dotenv()


def _env(key: str, yaml_val: str, default: str) -> str:
    v = os.environ.get(key)
    if v is not None:
        return v
    if yaml_val:
        return yaml_val
    return default


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8081

    @classmethod
    def from_yaml(cls, data: dict | None = None) -> "ApiConfig":
        if not data:
            return cls()
        return cls(
            host=data.get("host", "0.0.0.0"),
            port=int(data.get("port", 8081)),
        )


@dataclass
class LoggingConfig:
    level: str = "INFO"

    @classmethod
    def from_yaml(cls, data: dict | None = None) -> "LoggingConfig":
        if not data:
            return cls()
        return cls(level=data.get("level", "INFO"))


@dataclass
class LiveKitConfig:
    api_key: str = ""
    api_secret: str = ""
    room_name: str = "ai-assistant-room"

    @classmethod
    def from_yaml(cls, data: dict | None = None) -> "LiveKitConfig":
        if not data:
            return cls()
        return cls(
            api_key=_env("LIVEKIT_API_KEY", data.get("api_key", ""), "devkey"),
            api_secret=_env("LIVEKIT_API_SECRET", data.get("api_secret", ""), "devkey_secret"),
            room_name=_env("EIDOLON_ESP32_ROOM_NAME", data.get("room_name", ""), "ai-assistant-room"),
        )


@dataclass
class Esp32Config:
    server_url: str = "ws://localhost:7880"

    @classmethod
    def from_yaml(cls, data: dict | None = None) -> "Esp32Config":
        if not data:
            return cls()
        return cls(
            server_url=_env("EIDOLON_LIVEKIT_URL", data.get("server_url", ""), "wss://livekit.eidolon.yangtzeailab.com"),
        )


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    livekit: LiveKitConfig = field(default_factory=LiveKitConfig)
    esp32: Esp32Config = field(default_factory=Esp32Config)

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "AppConfig":
        import yaml

        data: dict = {}
        if path and path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        return cls(
            api=ApiConfig.from_yaml(data.get("api")),
            logging=LoggingConfig.from_yaml(data.get("logging")),
            livekit=LiveKitConfig.from_yaml(data.get("livekit")),
            esp32=Esp32Config.from_yaml(data.get("esp32")),
        )


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "default.yaml"


def load_config(config_path: Path | None = None) -> AppConfig:
    return AppConfig.from_yaml(config_path or DEFAULT_CONFIG_PATH)
