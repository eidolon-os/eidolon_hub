"""Infrastructure resources selected by deployment configuration."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.runtime import LocalProcessLock, SecureIdGenerator, SystemClock
from hub.config import HubConfig
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryRepository

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    management_jwt: bytes
    device_registry_reader_token: str
    provider_token: str


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    repositories: SqlHubRepositories
    directory: DeviceDirectoryRepository
    http_client: httpx.AsyncClient
    clock: Clock
    ids: IdGenerator


def load_runtime_secrets() -> RuntimeSecrets:
    return RuntimeSecrets(
        management_jwt=_required_bytes("EIDOLON_HUB_MANAGEMENT_JWT_SECRET"),
        device_registry_reader_token=_required_text(
            "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN"
        ),
        provider_token=_required_text("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN"),
    )


async def open_runtime_resources(
    config: HubConfig,
    stack: AsyncExitStack,
) -> RuntimeResources:
    database_path = resolve_database_path(config)
    process_lock = LocalProcessLock(f"{database_path}.lock")
    process_lock.acquire()
    stack.callback(process_lock.release)
    database = create_database(config)
    stack.push_async_callback(database.close)
    await database.initialize_schema()

    # Provider egress is an explicit contract boundary. Inheriting ambient
    # proxy settings can silently redirect credentials and makes provider
    # egress depend on process-global configuration.
    http_client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
    repositories = SqlHubRepositories(database)
    directory: DeviceDirectoryRepository = InMemoryDeviceDirectoryRepository()

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
    """Create the local SQLite adapter without opening application services."""

    return HubDatabase.sqlite(resolve_database_path(config))


def resolve_database_path(config: HubConfig) -> Path:
    path = Path(config.persistence.path).expanduser()
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()
