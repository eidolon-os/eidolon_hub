"""Accounts and imports for account-backed Providers; the glue the HTTP layer calls.

Binds through a ``ProviderIntegration``, keeps the observation stream of every
bound account running into the runtime's cache, and syncs discovered devices
into Data through the importer. Knows nothing about any one ecosystem.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping
from typing import Any

from hub.integration.accounts import ProviderAccountStore

from .importer import RegistryImporter
from .ports import BindError, ProviderIntegration
from .runtime import SmartHomeRuntime

logger = logging.getLogger("hub.smarthome.accounts")


class BindRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class AccountService:
    def __init__(
        self,
        *,
        runtime: SmartHomeRuntime,
        integrations: Mapping[str, ProviderIntegration],
        accounts: ProviderAccountStore,
        importer: RegistryImporter,
    ) -> None:
        self._runtime = runtime
        self._integrations = dict(integrations)
        self._accounts = accounts
        self._importer = importer
        self._observers: dict[str, asyncio.Task] = {}

    def providers(self) -> dict[str, Any]:
        return {
            "providers": [
                integration.account_schema().model_dump(mode="json")
                for integration in self._integrations.values()
            ]
        }

    async def start(self) -> None:
        """Reconnect accounts bound before this process started and resume observing."""
        for integration in self._integrations.values():
            try:
                await integration.restore()
            except Exception:
                logger.exception("provider %s failed to restore its accounts", integration.kind)
        for record in await self._accounts.all():
            if record.status in ("connected", "degraded") and record.kind in self._integrations:
                self._observe(record.owner_id, record.account_id, record.kind)

    async def stop(self) -> None:
        for task in self._observers.values():
            task.cancel()
        for task in self._observers.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._observers.clear()

    async def list(self, owner_id: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": r.account_id,
                "kind": r.kind,
                "label": r.label,
                "status": r.status,
                "last_seen_ms": r.last_seen_ms,
                "error": r.error,
                "choices": list(r.choices),
            }
            for r in await self._accounts.list(owner_id)
        ]

    async def bind(
        self, owner_id: str, kind: str, account_id: str | None, fields: Mapping[str, str]
    ) -> dict[str, Any]:
        integration = self._integrations.get(kind)
        if integration is None:
            raise BindRefused("UNKNOWN_PROVIDER", f"no provider of kind {kind!r} is enabled")
        account_id = account_id or f"acc_{secrets.token_hex(4)}"
        existing = await self._accounts.get(owner_id, account_id)
        if existing is not None and existing.kind != kind:
            raise BindRefused("ACCOUNT_KIND_MISMATCH", "that account belongs to another provider")
        try:
            account = await integration.bind(owner_id, account_id, fields)
        except BindError as exc:
            raise BindRefused(exc.code, exc.message) from exc
        if account.status == "connected":
            self._observe(owner_id, account_id, kind)
        return account.model_dump(mode="json")

    async def unbind(self, owner_id: str, account_id: str) -> bool:
        record = await self._accounts.get(owner_id, account_id)
        if record is None:
            return False
        task = self._observers.pop(account_id, None)
        if task is not None:
            task.cancel()
        integration = self._integrations.get(record.kind)
        if integration is not None:
            await integration.unbind(owner_id, account_id)
        else:
            await self._accounts.delete(owner_id, account_id)
        return True

    async def sync(self, owner_id: str, account_id: str) -> dict[str, Any]:
        record = await self._accounts.get(owner_id, account_id)
        if record is None:
            raise KeyError(account_id)
        integration = self._integrations.get(record.kind)
        if integration is None:
            raise BindRefused("UNKNOWN_PROVIDER", f"provider {record.kind!r} is not enabled")
        try:
            discovered = await integration.discover(owner_id, account_id)
        except BindError as exc:
            raise BindRefused(exc.code, exc.message) from exc
        report = await self._importer.sync(owner_id, f"{record.kind}:{account_id}", discovered)
        # Reachability is known right away; the observer keeps it current after.
        provider = f"{record.kind}:{account_id}"
        registry = await self._runtime.snapshot(owner_id)
        by_ref = {
            d["provider_ref"]: d["device_id"]
            for d in registry["registry"]["devices"]
            if d["provider"] == provider
        }
        for found in discovered:
            device_id = by_ref.get(found.external_ref)
            if device_id is not None:
                await self._runtime.observe(
                    owner_id, device_id, reachable=found.reachable, state=None
                )
        return report.as_dict()

    def _observe(self, owner_id: str, account_id: str, kind: str) -> None:
        if account_id in self._observers and not self._observers[account_id].done():
            return
        integration = self._integrations[kind]
        provider = f"{kind}:{account_id}"

        async def run() -> None:
            async for observation in integration.observe(owner_id, account_id):
                snapshot = await self._runtime.snapshot(owner_id)
                device_id = next(
                    (
                        d["device_id"]
                        for d in snapshot["registry"]["devices"]
                        if d["provider"] == provider and d["provider_ref"] == observation.device_id
                    ),
                    None,
                )
                if device_id is not None:
                    await self._runtime.observe(
                        owner_id,
                        device_id,
                        reachable=observation.reachable,
                        state=observation.state,
                    )

        self._observers[account_id] = asyncio.create_task(
            run(), name=f"smarthome-observe-{account_id}"
        )
