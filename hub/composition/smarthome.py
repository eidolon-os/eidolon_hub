"""Compose the smart-home runtime: providers by configuration, primitives by state path.

Business logic stays in ``hub.smarthome``; this is the only place that knows
which adapters exist and how their dependencies are built.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from hub.integration.accounts import ProviderAccountStore
from hub.integration.ledger import SqliteReceiptLedger
from hub.integration.observation import SqliteObservationCache
from hub.integration.registry import ProviderRegistry
from hub.integration.store import IntegrationStore
from hub.integration.vault import CredentialVault, load_vault_key
from hub.smarthome.http_registry import HttpRegistrySource
from hub.smarthome.importer import HttpRegistryWriter, RegistryImporter
from hub.smarthome.integration_service import AccountService
from hub.smarthome.ports import ProviderIntegration
from hub.smarthome.providers.virtual import VirtualProvider
from hub.smarthome.providers.zhoubian.provider import ZhoubianProvider
from hub.smarthome.runtime import SmartHomeRuntime

ACCOUNT_PROVIDER_KINDS = frozenset({"zhoubian"})


@dataclass(frozen=True, slots=True)
class SmartHome:
    runtime: SmartHomeRuntime
    accounts: AccountService | None


def build_smarthome(
    config, client: httpx.AsyncClient, environ, host_identity: str = ""
) -> SmartHome | None:
    if config.smarthome.workspace_url is None:
        return None
    for key in ("EIDOLON_HUB_SMARTHOME_TOKEN", "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN"):
        if len(environ.get(key, "")) < 32:
            raise RuntimeError(f"{key} must contain at least 32 characters")
    state_dir = Path(config.persistence.path).expanduser().parent
    virtual_path = Path(config.smarthome.state_path or state_dir / "smarthome.sqlite3").expanduser()
    legacy = state_dir.parent / "channel/smarthome.sqlite3"
    if config.smarthome.state_path is None and legacy.exists() and not virtual_path.exists():
        raise RuntimeError(
            "existing smart-home state requires offline migration from Channel to Hub"
        )

    enabled = list(config.smarthome.providers)
    integration_store = IntegrationStore(
        Path(config.smarthome.integration_path or state_dir / "integration.sqlite3").expanduser()
    )
    integration_store.initialize()
    accounts_store = ProviderAccountStore(integration_store)
    vault: CredentialVault | None = None
    if ACCOUNT_PROVIDER_KINDS & set(enabled):
        vault = CredentialVault(integration_store, load_vault_key(environ))

    registry = ProviderRegistry()

    def virtual() -> VirtualProvider:
        provider = VirtualProvider(virtual_path)
        provider.initialize()
        return provider

    registry.register("virtual", virtual)
    registry.register(
        "zhoubian",
        lambda: ZhoubianProvider(
            client=client,
            vault=vault,
            accounts=accounts_store,
            host_identity=host_identity or os.uname().nodename,
        ),
    )
    providers = registry.build(enabled)

    workspace_token = environ["EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN"]
    runtime = SmartHomeRuntime(
        registry=HttpRegistrySource(client, config.smarthome.workspace_url, workspace_token),
        providers=providers,
        ledger=SqliteReceiptLedger(integration_store),
        observations=SqliteObservationCache(integration_store),
    )
    integrations: dict[str, ProviderIntegration] = {
        kind: provider for kind, provider in providers.items() if kind in ACCOUNT_PROVIDER_KINDS
    }
    accounts = None
    if integrations:
        accounts = AccountService(
            runtime=runtime,
            integrations=integrations,
            accounts=accounts_store,
            importer=RegistryImporter(
                HttpRegistryWriter(client, config.smarthome.workspace_url, workspace_token)
            ),
        )
    return SmartHome(runtime=runtime, accounts=accounts)
