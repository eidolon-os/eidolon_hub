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
    api_key: str = ""
    api_secret: str = ""

    @classmethod
    def from_env(cls) -> "LiveKitConfig":
        return cls(
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
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    livekit: LiveKitConfig = field(default_factory=LiveKitConfig)
    esp32: Esp32Config = field(default_factory=Esp32Config)

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            api=ApiConfig.from_env(),
            logging=LoggingConfig.from_env(),
            livekit=LiveKitConfig.from_env(),
            esp32=Esp32Config.from_env(),
        )


def load_config() -> AppConfig:
    return AppConfig.load()
