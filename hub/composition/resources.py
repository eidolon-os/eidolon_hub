"""Infrastructure resources selected by deployment configuration."""

from __future__ import annotations

import base64
import json
import os
import stat
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.runtime import LocalProcessLock, SecureIdGenerator, SystemClock
from hub.admission.application import (
    CommissioningProofVerifier,
    HmacCommissioningProofVerifier,
    RejectingCommissioningProofVerifier,
)
from hub.config import HubConfig
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryRepository

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    management_jwt: bytes
    device_registry_reader_token: str


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    database: HubDatabase
    repositories: SqlHubRepositories
    directory: DeviceDirectoryRepository
    http_client: httpx.AsyncClient
    clock: Clock
    ids: IdGenerator
    commissioning_proofs: CommissioningProofVerifier
    commissioning_ready: bool


def load_runtime_secrets() -> RuntimeSecrets:
    return RuntimeSecrets(
        management_jwt=_required_bytes("EIDOLON_HUB_MANAGEMENT_JWT_SECRET"),
        device_registry_reader_token=_required_text(
            "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN"
        ),
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
    commissioning_proofs, commissioning_ready = load_commissioning_proof_verifier(config)

    return RuntimeResources(
        database=database,
        repositories=repositories,
        directory=directory,
        http_client=http_client,
        clock=SystemClock(),
        ids=SecureIdGenerator(),
        commissioning_proofs=commissioning_proofs,
        commissioning_ready=commissioning_ready,
    )


def load_commissioning_proof_verifier(
    config: HubConfig,
) -> tuple[CommissioningProofVerifier, bool]:
    """Load the explicit development registry or fail closed for production."""

    proof = config.commissioning_proof
    if proof.profile != "development-hmac":
        return RejectingCommissioningProofVerifier(), False
    path = Path(proof.setup_secret_registry_path or "")
    if not path.is_file():
        return RejectingCommissioningProofVerifier(), False
    metadata = path.stat()
    if metadata.st_uid != 0 or not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o037:
        raise RuntimeError(
            "development commissioning registry must be root-owned and inaccessible to others"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("profile") != "eidolon-development-hmac-commissioning-v1":
            raise ValueError("development commissioning registry profile differs")
        encoded = document["devices"]
        if not isinstance(encoded, dict) or not encoded:
            raise ValueError("development commissioning registry has no devices")
        secrets_by_device = {
            str(device_id): base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4))
            for device_id, value in encoded.items()
        }
        if any(
            not device_id.strip() or len(secret) < 16
            for device_id, secret in secrets_by_device.items()
        ):
            raise ValueError("development commissioning registry entry is invalid")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("development commissioning registry is invalid") from exc
    return (
        HmacCommissioningProofVerifier(secrets_by_device.get),
        True,
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

    return HubDatabase.sqlite(
        resolve_database_path(config),
        owner_domain_id=config.onboarding.owner_domain_id,
        owner_domain_generation=config.onboarding.owner_domain_generation,
        authority_anchor_path=config.persistence.authority_anchor_path,
        authority_bootstrap_path=config.persistence.authority_bootstrap_path,
    )


def resolve_database_path(config: HubConfig) -> Path:
    path = Path(config.persistence.path).expanduser()
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()
