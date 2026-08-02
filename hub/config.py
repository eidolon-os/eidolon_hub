"""Strict deployment configuration for the Eidolon Hub control plane."""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_ROOT = _REPO_ROOT / "config"
_INSTALLED_CONFIG_ROOT = Path(sysconfig.get_path("data")) / "config"
_DEFAULT_ENV = _CONFIG_ROOT / ".env"
_PROFILE_ENV = "EIDOLON_HUB_PROFILE"
_SETTINGS_ENV = "EIDOLON_HUB_SETTINGS_YAML"
_VALID_PROFILES = frozenset({"local", "cloud"})


class _StrictConfig(BaseModel):
    """One declarative source of defaults, types and unknown-field rejection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DeploymentConfig(_StrictConfig):
    mode: Literal["local", "cloud"] = "local"


class ObservabilityConfig(_StrictConfig):
    enabled: bool = False
    service_name: str = Field(default="eidolon-hub", min_length=1, max_length=128)


class MdnsDiscoveryConfig(_StrictConfig):
    enabled: bool = True


class DiscoveryConfig(_StrictConfig):
    mdns: MdnsDiscoveryConfig = Field(default_factory=MdnsDiscoveryConfig)


class DeviceAccessConfig(_StrictConfig):
    hub_id: str = Field(default="eidolon-hub-local", min_length=1, max_length=128)
    public_base_url: str = "https://eidolon-hub.local"
    session_lease_seconds: int = Field(default=45, ge=15, le=3600)
    heartbeat_after_ms: int = Field(default=15_000, ge=1000, le=300_000)

    @model_validator(mode="after")
    def validate_access_contract(self) -> DeviceAccessConfig:
        parsed = urlparse(self.public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("device_access.public_base_url must be a plain HTTPS base URL")
        if self.heartbeat_after_ms * 3 > self.session_lease_seconds * 1000:
            raise ValueError(
                "device heartbeat interval must not exceed one third of its session lease"
            )
        return self


class ChannelProviderConfig(_StrictConfig):
    contract_url: str = "http://127.0.0.1:8090/v1"

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


class SqlitePersistenceConfig(_StrictConfig):
    adapter: Literal["sqlite"] = "sqlite"
    path: str = Field(default="var/eidolon-hub.sqlite3", min_length=1)
    migrate_on_startup: bool = True


class PostgresqlPersistenceConfig(_StrictConfig):
    adapter: Literal["postgresql"] = "postgresql"
    dsn: str = "postgresql://127.0.0.1:5432/eidolon_hub"
    migrate_on_startup: bool = False
    pool_size: PositiveInt = 10
    max_overflow: int = Field(default=20, ge=0)
    pool_timeout_seconds: PositiveFloat = 10.0
    pool_recycle_seconds: PositiveInt = 1800

    @model_validator(mode="after")
    def validate_non_secret_dsn(self) -> PostgresqlPersistenceConfig:
        parsed = urlparse(self.dsn)
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if (
            parsed.scheme != "postgresql"
            or not parsed.hostname
            or not parsed.path.strip("/")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or query_keys & {"user", "username", "pass", "password"}
        ):
            raise ValueError("persistence.dsn must be a credential-free PostgreSQL database URL")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("persistence.dsn contains an invalid PostgreSQL port") from exc
        return self


PersistenceConfig = Annotated[
    SqlitePersistenceConfig | PostgresqlPersistenceConfig,
    Field(discriminator="adapter"),
]


class DeviceDirectoryConfig(_StrictConfig):
    projection_interval_seconds: PositiveFloat = 5.0
    cache_refresh_seconds: PositiveFloat | None = None


class HubConfig(_StrictConfig):
    """Only deployment-independent behavior and external contract addresses."""

    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    persistence: PersistenceConfig = Field(default_factory=SqlitePersistenceConfig)
    device_directory: DeviceDirectoryConfig = Field(default_factory=DeviceDirectoryConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    device_access: DeviceAccessConfig = Field(default_factory=DeviceAccessConfig)
    channel_provider: ChannelProviderConfig = Field(default_factory=ChannelProviderConfig)

    @model_validator(mode="after")
    def validate_deployment(self) -> HubConfig:
        public_hostname = urlparse(self.device_access.public_base_url).hostname or ""
        if self.deployment.mode == "cloud":
            if self.discovery.mdns.enabled:
                raise ValueError("cloud deployment cannot enable link-local mDNS")
            if self.persistence.adapter != "postgresql":
                raise ValueError("cloud deployment requires PostgreSQL persistence")
        if self.discovery.mdns.enabled and not public_hostname.endswith(".local"):
            raise ValueError("enabled mDNS requires a .local public_base_url hostname")
        return self

    @classmethod
    def load(cls) -> HubConfig:
        _bootstrap_dotenv()
        requested_profile, settings_path = _resolve_settings_yaml()
        source = _load_yaml(settings_path)
        if "deployment" not in source:
            raise ValueError("Hub settings must explicitly declare deployment.mode")
        config = cls.model_validate(source)
        if requested_profile is not None and config.deployment.mode != requested_profile:
            raise ValueError(
                f"selected profile {requested_profile!r} contains mode {config.deployment.mode!r}"
            )
        return config


def _resolve_profile() -> str:
    profile = os.environ.get(_PROFILE_ENV, "local").strip().lower() or "local"
    if profile not in _VALID_PROFILES:
        raise ValueError(f"{_PROFILE_ENV} must be local or cloud")
    return profile


def _resolve_settings_yaml() -> tuple[str | None, Path]:
    explicit = os.environ.get(_SETTINGS_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        requested_profile: str | None = None
    else:
        requested_profile = _resolve_profile()
        filename = f"settings.{requested_profile}.yaml"
        source_root = (
            _CONFIG_ROOT if (_CONFIG_ROOT / filename).is_file() else _INSTALLED_CONFIG_ROOT
        )
        path = source_root / filename
    if not path.is_file():
        raise FileNotFoundError(f"Hub settings file is missing: {path}")
    return requested_profile, path.resolve()


def _bootstrap_dotenv() -> None:
    """Load a dotenv when requested; cloud environments need no dotenv file."""

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
