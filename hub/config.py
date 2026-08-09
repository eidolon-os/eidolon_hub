"""Strict local configuration for the Eidolon Hub control plane."""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_ROOT = _REPO_ROOT / "config"
_INSTALLED_CONFIG_ROOT = Path(sysconfig.get_path("data")) / "config"
_DEFAULT_ENV = _CONFIG_ROOT / ".env"
_SETTINGS_ENV = "EIDOLON_HUB_SETTINGS_YAML"


class _StrictConfig(BaseModel):
    """One declarative source of defaults, types and unknown-field rejection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MdnsDiscoveryConfig(_StrictConfig):
    enabled: bool = True


class DiscoveryConfig(_StrictConfig):
    mdns: MdnsDiscoveryConfig = Field(default_factory=MdnsDiscoveryConfig)


class OnboardingConfig(_StrictConfig):
    hub_id: str = Field(default="eidolon-hub-local", min_length=1, max_length=128)
    public_base_url: str = "https://eidolon-hub.local"
    retrieval_window_seconds: int = Field(default=1800, ge=60, le=86_400)

    @model_validator(mode="after")
    def validate_onboarding_contract(self) -> OnboardingConfig:
        parsed = urlparse(self.public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("onboarding.public_base_url must be a plain HTTPS base URL")
        return self


class ChannelProviderConfig(_StrictConfig):
    contract_url: str = "http://127.0.0.1:8767/v1"

    @model_validator(mode="after")
    def validate_contract_url(self) -> ChannelProviderConfig:
        parsed = urlparse(self.contract_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("channel_provider.contract_url must be a plain HTTP(S) base URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("remote Channel Provider must use HTTPS")
        return self


class PersistenceConfig(_StrictConfig):
    path: str = Field(
        default="/Users/manson/eidolon/data/eidolon-hub.sqlite3",
        min_length=1,
    )


class HubConfig(_StrictConfig):
    """Local behavior and external contract addresses."""

    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    channel_provider: ChannelProviderConfig = Field(default_factory=ChannelProviderConfig)

    @classmethod
    def load(cls) -> HubConfig:
        _bootstrap_dotenv()
        settings_path = _resolve_settings_yaml()
        source = _load_yaml(settings_path)
        return cls.model_validate(source)


def _resolve_settings_yaml() -> Path:
    explicit = os.environ.get(_SETTINGS_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    else:
        filename = "settings.yaml"
        source_root = (
            _CONFIG_ROOT if (_CONFIG_ROOT / filename).is_file() else _INSTALLED_CONFIG_ROOT
        )
        path = source_root / filename
    if not path.is_file():
        raise FileNotFoundError(f"Hub settings file is missing: {path}")
    return path.resolve()


def _bootstrap_dotenv() -> None:
    """Load a dotenv when requested or when the local default exists."""

    from dotenv import load_dotenv

    explicit = os.environ.get("EIDOLON_HUB_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Hub environment file is missing: {path}")
        load_dotenv(path.resolve(), override=False)
    elif _DEFAULT_ENV.is_file():
        load_dotenv(_DEFAULT_ENV, override=False)


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("Hub settings must be a YAML object")
    return value


def load_hub_config() -> HubConfig:
    return HubConfig.load()
