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
    owner_domain_id: str = Field(default="owner-local", min_length=1, max_length=128)
    trust_epoch: int = Field(default=1, ge=1)
    descriptor_uri: str = (
        "https://eidolon-hub.local/api/device-onboarding/v1/descriptor"
    )
    descriptor_path: str = "/etc/eidolon/owner-domain/owner_domain_descriptor.json"
    owner_root_certificate_path: str = (
        "/etc/eidolon/owner-domain/owner_domain_root_ca.pem"
    )
    authority_signing_certificate_path: str = (
        "/etc/eidolon/owner-domain/authority_signing_certificate.pem"
    )
    retrieval_window_seconds: int = Field(default=1800, ge=60, le=86_400)

    @model_validator(mode="after")
    def validate_onboarding_contract(self) -> OnboardingConfig:
        parsed = urlparse(self.descriptor_uri)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/api/device-onboarding/v1/descriptor"
        ):
            raise ValueError("onboarding.descriptor_uri must be the plain HTTPS descriptor URI")
        for value, name in (
            (self.descriptor_path, "descriptor_path"),
            (self.owner_root_certificate_path, "owner_root_certificate_path"),
            (
                self.authority_signing_certificate_path,
                "authority_signing_certificate_path",
            ),
        ):
            if not Path(value).is_absolute():
                raise ValueError(f"onboarding.{name} must be absolute")
        return self


class ChannelProviderConfig(_StrictConfig):
    contract_url: str = "http://127.0.0.1:8767/v1"
    revoke_delivery_poll_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    revoke_retry_base_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    revoke_retry_max_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)

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
        if self.revoke_retry_max_seconds < self.revoke_retry_base_seconds:
            raise ValueError("revoke retry maximum must not be below the base delay")
        return self


class PersistenceConfig(_StrictConfig):
    path: str = Field(
        default_factory=lambda: str(
            Path(os.environ.get("EIDOLON_STATE_ROOT", "~/eidolon/data")).expanduser()
            / "hub/eidolon-hub.sqlite3"
        ),
        min_length=1,
    )


class DeviceControlConfig(_StrictConfig):
    erase_operation_ttl_seconds: int = Field(default=604_800, ge=300, le=2_592_000)
    erase_reconcile_poll_seconds: float = Field(default=1.0, ge=0.1, le=60.0)


class HubConfig(_StrictConfig):
    """Local behavior and external contract addresses."""

    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    channel_provider: ChannelProviderConfig = Field(default_factory=ChannelProviderConfig)
    device_control: DeviceControlConfig = Field(default_factory=DeviceControlConfig)

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
    persistence = value.get("persistence")
    if isinstance(persistence, dict) and isinstance(persistence.get("path"), str):
        persistence["path"] = os.path.expandvars(os.path.expanduser(persistence["path"]))
    return value


def load_hub_config() -> HubConfig:
    return HubConfig.load()
