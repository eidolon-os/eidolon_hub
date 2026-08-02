"""Infrastructure resources selected by deployment configuration."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy.engine import make_url

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import CachedDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.runtime import SecureIdGenerator, SystemClock
from hub.config import HubConfig
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryRepository

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    lease: bytes
    management_jwt: bytes
    provider_token: str


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    repositories: SqlHubRepositories
    directory: DeviceDirectoryRepository
    http_client: httpx.AsyncClient
    clock: Clock
    ids: IdGenerator


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    hub_instance_id: str
    otlp_endpoint: str


def load_runtime_secrets() -> RuntimeSecrets:
    return RuntimeSecrets(
        lease=_required_bytes("EIDOLON_HUB_LEASE_SECRET"),
        management_jwt=_required_bytes("EIDOLON_HUB_MANAGEMENT_JWT_SECRET"),
        provider_token=_required_text("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN"),
    )


def load_runtime_environment(config: HubConfig) -> RuntimeEnvironment:
    instance_id = os.environ.get("EIDOLON_HUB_INSTANCE_ID", "").strip()
    if not instance_id and config.deployment.mode == "local":
        instance_id = f"{config.device_access.hub_id}-instance-1"
    if not instance_id:
        raise RuntimeError("EIDOLON_HUB_INSTANCE_ID is required in cloud mode")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if config.observability.enabled and not endpoint:
        raise RuntimeError("enabled observability requires OTEL_EXPORTER_OTLP_ENDPOINT")
    if config.observability.enabled:
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
            raise RuntimeError("OTEL_EXPORTER_OTLP_ENDPOINT must be an HTTP(S) endpoint")
    else:
        endpoint = ""
    return RuntimeEnvironment(hub_instance_id=instance_id, otlp_endpoint=endpoint)


async def open_runtime_resources(
    config: HubConfig,
    stack: AsyncExitStack,
) -> RuntimeResources:
    database = create_database(config)
    stack.push_async_callback(database.close)
    if config.persistence.migrate_on_startup:
        await database.migrate()
    else:
        await database.assert_schema_current()

    # Provider egress is an explicit contract boundary. Inheriting ambient
    # proxy settings can silently redirect credentials and makes local/cloud
    # behavior depend on process-global configuration.
    http_client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
    repositories = SqlHubRepositories(database)
    cached_directory = CachedDeviceDirectoryRepository(
        repositories.directory,
        refresh_interval_seconds=config.device_directory.cache_refresh_seconds,
    )
    await cached_directory.start()
    stack.push_async_callback(cached_directory.stop)
    directory: DeviceDirectoryRepository = cached_directory

    return RuntimeResources(
        repositories=repositories,
        directory=directory,
        http_client=http_client,
        clock=SystemClock(),
        ids=SecureIdGenerator(),
    )


def _required_bytes(env_name: str) -> bytes:
    value = os.environ.get(env_name, "").encode()
    if len(value) < 32:
        raise RuntimeError(f"{env_name} must contain at least 32 bytes")
    return value


def _required_text(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if len(value.encode()) < 32:
        raise RuntimeError(f"{env_name} must contain at least 32 bytes")
    return value


def create_database(config: HubConfig) -> HubDatabase:
    """Create the configured database adapter without opening application services."""

    persistence = config.persistence
    if persistence.adapter == "sqlite":
        path = Path(persistence.path).expanduser()
        if not path.is_absolute():
            data_root = Path(os.environ.get("EIDOLON_HUB_DATA_DIR", _REPO_ROOT)).expanduser()
            path = data_root / path
        return HubDatabase.sqlite(path)
    username = os.environ.get("EIDOLON_HUB_POSTGRES_USER", "").strip()
    password = os.environ.get("EIDOLON_HUB_POSTGRES_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "EIDOLON_HUB_POSTGRES_USER and EIDOLON_HUB_POSTGRES_PASSWORD are required"
        )
    credentialed_url = make_url(persistence.dsn).set(username=username, password=password)
    return HubDatabase.postgresql(
        credentialed_url.render_as_string(hide_password=False),
        pool_size=persistence.pool_size,
        max_overflow=persistence.max_overflow,
        pool_timeout_seconds=persistence.pool_timeout_seconds,
        pool_recycle_seconds=persistence.pool_recycle_seconds,
    )
