"""The Home Assistant Provider: accounts are instances, devices are exposed entities."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import aiohttp
from eidolon_sdk.biz.smarthome import (
    ERROR_DEVICE_OFFLINE,
    ERROR_PLATFORM_REJECTED,
    AccountSchema,
    Command,
    Device,
    DiscoveredDevice,
    Observation,
    ProviderAccount,
    SmartHomeError,
    provider_binding,
)

from hub.integration.accounts import AccountRecord, ProviderAccountStore
from hub.integration.vault import CredentialVault
from hub.smarthome.ports import BindError

from .connection import ConnectionFailed, HaConnection
from .mapping import Entity, discover, project_state, satisfied, sensor_entities, to_service_call

logger = logging.getLogger("hub.smarthome.homeassistant")

KIND = "homeassistant"
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0


class HomeAssistantProvider:
    """``SmartHomeProvider`` + ``ProviderIntegration``; event-confirming."""

    kind = KIND
    pushes_observations = True

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        vault: CredentialVault,
        accounts: ProviderAccountStore,
        clock=time.time,
    ) -> None:
        self._session = session
        self._vault = vault
        self._accounts = accounts
        self._clock = clock
        self._connections: dict[str, HaConnection] = {}
        self._owners: dict[str, str] = {}
        self._registry_cache: dict[str, dict[str, Entity]] = {}

    # --- ProviderIntegration -----------------------------------------------------

    def account_schema(self) -> AccountSchema:
        return AccountSchema(
            kind=KIND,
            label="Home Assistant",
            fields=(
                {"name": "url", "label": "Home Assistant 地址", "kind": "url"},
                {"name": "token", "label": "长效访问令牌", "kind": "secret"},
            ),
        )

    async def bind(
        self, owner_id: str, account_id: str, fields: Mapping[str, str]
    ) -> ProviderAccount:
        url, token = (fields.get("url") or "").strip(), (fields.get("token") or "").strip()
        if not url or not token:
            raise BindError("MISSING_FIELDS", "需要地址和长效访问令牌")
        if not url.startswith(("http://", "https://")):
            raise BindError("INVALID_URL", "地址要以 http:// 或 https:// 开头")
        connection = HaConnection(self._session, url, token)
        try:
            await connection.open()
        except ConnectionFailed as exc:
            raise BindError(exc.code, exc.message) from exc
        old = self._connections.pop(account_id, None)
        if old is not None:
            await old.close()
        self._connections[account_id] = connection
        self._owners[account_id] = owner_id
        await self._vault.put(account_id, "url", url)
        await self._vault.put(account_id, "token", token)
        record = AccountRecord(
            account_id,
            owner_id,
            KIND,
            connection.location_name[:64],
            "connected",
            last_seen_ms=self._now_ms(),
        )
        await self._accounts.upsert(record)
        return _account(record)

    async def unbind(self, owner_id: str, account_id: str) -> None:
        connection = self._connections.pop(account_id, None)
        if connection is not None:
            await connection.close()
        self._owners.pop(account_id, None)
        self._registry_cache.pop(account_id, None)
        await self._vault.delete_all(account_id)
        await self._accounts.delete(owner_id, account_id)

    async def restore(self) -> None:
        for record in await self._accounts.all():
            if record.kind != KIND or record.status == "revoked":
                continue
            fields = {
                name: await self._vault.get(record.account_id, name) or ""
                for name in ("url", "token")
            }
            try:
                await self.bind(record.owner_id, record.account_id, fields)
            except BindError as exc:
                logger.warning("home assistant account %s not restored: %s", record.account_id, exc)
                await self._accounts.set_status(
                    record.account_id, "degraded", error=exc.message[:200]
                )

    async def discover(self, owner_id: str, account_id: str) -> list[DiscoveredDevice]:
        connection = self._connection(owner_id, account_id)
        entities = await self._entities(account_id, connection)
        return discover(list(entities.values()))

    async def observe(self, owner_id: str, account_id: str) -> AsyncIterator[Observation]:
        connection = self._connection(owner_id, account_id)
        backoff = RECONNECT_MIN_S
        while account_id in self._connections:
            if not connection.connected:
                try:
                    await connection.open()
                    backoff = RECONNECT_MIN_S
                    await self._accounts.set_status(account_id, "connected", seen=True)
                    for entity_id, state in connection.states.items():
                        yield self._observation(account_id, entity_id, state)
                except ConnectionFailed as exc:
                    await self._accounts.set_status(account_id, "degraded", error=exc.message[:200])
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_S)
                    continue
            async for change in connection.changes():
                yield self._observation(account_id, change["entity_id"], change["new_state"])
                if not connection.connected:
                    break

    # --- SmartHomeProvider ----------------------------------------------------------

    async def reconcile(self, owner_id: str, devices: Sequence[Device]) -> None:
        return None

    async def states(self, owner_id: str, devices: Sequence[Device]) -> dict[str, dict[str, Any]]:
        return {}

    async def execute(self, owner_id: str, device: Device, command: Command) -> dict[str, Any]:
        _kind, account_id = provider_binding(device.provider)
        connection = self._connection(owner_id, account_id)
        if not connection.connected:
            raise SmartHomeError(ERROR_DEVICE_OFFLINE, "Home Assistant is not connected")
        entity_id = device.provider_ref or ""
        current = connection.states.get(entity_id)
        if current is None or current.get("state") in ("unavailable", "unknown"):
            raise SmartHomeError(ERROR_DEVICE_OFFLINE, f"{entity_id} is unavailable")
        before = project_state(device, self._entity(account_id, current))
        call = to_service_call(
            device,
            command,
            before,
            int((current.get("attributes") or {}).get("supported_features") or 0),
        )
        try:
            await connection.call_service(call.domain, call.service, dict(call.data))
        except ConnectionFailed as exc:
            raise SmartHomeError(ERROR_PLATFORM_REJECTED, exc.message) from exc

        def done(state: dict[str, Any]) -> bool:
            return satisfied(
                command, before, project_state(device, self._entity(account_id, state))
            )

        # Event-confirming: the returned state is the entity's after it changed.
        # The runtime's deadline bounds this wait and turns a timeout into ``unknown``.
        confirmed = await connection.wait_state(entity_id, done, timeout=3600)
        return project_state(device, self._entity(account_id, confirmed))

    # --- internals -------------------------------------------------------------------

    def _connection(self, owner_id: str, account_id: str) -> HaConnection:
        connection = self._connections.get(account_id)
        if connection is None or self._owners.get(account_id) != owner_id:
            raise SmartHomeError(ERROR_DEVICE_OFFLINE, f"account {account_id} is not bound")
        return connection

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    async def _entities(self, account_id: str, connection: HaConnection) -> dict[str, Entity]:
        registries = await connection.registries()
        areas = {a["area_id"]: a["name"] for a in registries["areas"]}
        devices = {d["id"]: d for d in registries["devices"]}
        exposed = {eid for eid, flags in registries["exposed"].items() if flags.get("conversation")}
        entries = {e["entity_id"]: e for e in registries["entities"]}
        entities: dict[str, Entity] = {}
        for entity_id in exposed:
            state = connection.states.get(entity_id)
            if state is None:
                continue
            entry = entries.get(entity_id, {})
            device = devices.get(entry.get("device_id") or "", {})
            area_id = entry.get("area_id") or device.get("area_id")
            entities[entity_id] = Entity(
                entity_id=entity_id,
                state=str(state.get("state")),
                attributes=state.get("attributes") or {},
                name=entry.get("name")
                or entry.get("original_name")
                or state.get("attributes", {}).get("friendly_name")
                or entity_id,
                area_name=areas.get(area_id) if area_id else None,
                device_id=entry.get("device_id"),
                device_class=(state.get("attributes") or {}).get("device_class")
                or entry.get("original_device_class"),
            )
        self._registry_cache[account_id] = entities
        return entities

    def _entity(self, account_id: str, state: dict[str, Any]) -> Entity:
        known = self._registry_cache.get(account_id, {}).get(state["entity_id"])
        attributes = state.get("attributes") or {}
        return Entity(
            entity_id=state["entity_id"],
            state=str(state.get("state")),
            attributes=attributes,
            name=known.name if known else attributes.get("friendly_name") or state["entity_id"],
            area_name=known.area_name if known else None,
            device_id=known.device_id if known else None,
            device_class=attributes.get("device_class") or (known.device_class if known else None),
        )

    def _observation(
        self, account_id: str, entity_id: str, state: dict[str, Any] | None
    ) -> Observation:
        reachable = state is not None and state.get("state") not in ("unavailable", "unknown")
        return Observation(
            device_id=entity_id, reachable=reachable, state=None, observed_at_ms=self._now_ms()
        )

    def project(self, device: Device, account_id: str) -> dict[str, Any] | None:
        """The current SDK state of a device from the cache, for observations."""
        connection = self._connections.get(account_id)
        if connection is None or not device.provider_ref:
            return None
        if device.type == "sensor":
            parts = [connection.states.get(eid) for eid in sensor_entities(device.provider_ref)]
            parts = [p for p in parts if p is not None]
            if not parts:
                return None
            return project_state(
                device,
                self._entity(account_id, parts[0]),
                [self._entity(account_id, p) for p in parts[1:]],
            )
        state = connection.states.get(device.provider_ref)
        if state is None:
            return None
        return project_state(device, self._entity(account_id, state))


def _account(record: AccountRecord) -> ProviderAccount:
    return ProviderAccount(
        account_id=record.account_id,
        kind=record.kind,
        label=record.label,
        status=record.status,
        last_seen_ms=record.last_seen_ms,
        error=record.error,
        choices=tuple(record.choices),
    )
