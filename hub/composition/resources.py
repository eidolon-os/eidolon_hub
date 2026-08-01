"""Infrastructure resources selected by deployment configuration."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import CachedDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.runtime import SecureIdGenerator, SystemClock
from hub.config import HubConfig
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryRepository


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


def load_runtime_secrets() -> RuntimeSecrets:
    return RuntimeSecrets(
        lease=_required_bytes("EIDOLON_HUB_LEASE_SECRET"),
        management_jwt=_required_bytes("EIDOLON_HUB_MANAGEMENT_JWT_SECRET"),
        provider_token=_required_text("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN"),
    )


async def open_runtime_resources(
    config: HubConfig,
    stack: AsyncExitStack,
) -> RuntimeResources:
    database = _database(config)
    stack.push_async_callback(database.close)
    if config.persistence.init_schema:
        await database.init_schema()

    # Provider egress is an explicit contract boundary. Inheriting ambient
    # proxy settings can silently redirect credentials and makes local/cloud
    # behavior depend on process-global configuration.
    http_client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
    repositories = SqlHubRepositories(database)
    directory: DeviceDirectoryRepository = repositories.directory
    if config.persistence.directory_cache_enabled:
        cached_directory = CachedDeviceDirectoryRepository(
            repositories.directory,
            reconciliation_seconds=config.persistence.reconciliation_seconds,
        )
        await cached_directory.start()
        stack.push_async_callback(cached_directory.stop)
        directory = cached_directory

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


def _database(config: HubConfig) -> HubDatabase:
    persistence = config.persistence
    if persistence.adapter == "sqlite":
        return HubDatabase.sqlite(persistence.sqlite_path)
    dsn = os.environ.get(persistence.postgresql_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(f"{persistence.postgresql_dsn_env} is required")
    return HubDatabase.postgresql(
        dsn,
        pool_size=persistence.pool_size,
        max_overflow=persistence.max_overflow,
    )
