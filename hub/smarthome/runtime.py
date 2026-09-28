"""Owner-scoped device execution. No transport, panel or model dependencies."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from eidolon_sdk.biz.smarthome import (
    ERROR_DEADLINE_EXCEEDED,
    ERROR_DEVICE_OFFLINE,
    ERROR_UNKNOWN_DEVICE,
    ERROR_UNKNOWN_SCENE,
    Command,
    CommandResult,
    ExecuteRequest,
    ExecuteResult,
    Registry,
    SmartHomeError,
    validate_command,
    validate_state,
)

from .ports import RegistrySource, SmartHomeProvider

logger = logging.getLogger("hub.smarthome")
IDEMPOTENCY_TTL_MS = 10 * 60_000
IDEMPOTENCY_CAPACITY = 1024


class IdempotencyConflict(ValueError):
    """A request_id was reused for different commands or a different scene.

    Answering with the first result would report commands that never ran.
    """


@dataclass(frozen=True, slots=True)
class _Recorded:
    expires_at_ms: int
    fingerprint: str
    result: ExecuteResult


@dataclass
class _Owner:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    registry: Registry | None = None
    # Last state each Provider reported, for devices a registered Provider serves.
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    recorded: OrderedDict[tuple[str, str], _Recorded] = field(default_factory=OrderedDict)


class SmartHomeRuntime:
    def __init__(
        self,
        *,
        registry: RegistrySource,
        providers: Mapping[str, SmartHomeProvider],
        now_ms: Callable[[], int] | None = None,
        idempotency_ttl_ms: int = IDEMPOTENCY_TTL_MS,
        idempotency_capacity: int = IDEMPOTENCY_CAPACITY,
    ):
        self._registry_source = registry
        self._providers = dict(providers)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._ttl_ms = idempotency_ttl_ms
        self._capacity = idempotency_capacity
        self._owners: dict[str, _Owner] = {}

    async def execute(self, owner_id: str, request: ExecuteRequest) -> ExecuteResult:
        scope = request.origin.device_ref if request.origin.kind == "touch" else ""
        return await self._execute(owner_id, request, scope=scope or "")

    async def _execute(
        self, owner_id: str, request: ExecuteRequest, *, scope: str
    ) -> ExecuteResult:
        key = (scope, request.request_id)
        fingerprint = _fingerprint(request)
        owner = self._owner(owner_id)
        async with owner.lock:
            recorded = self._lookup(owner, key, fingerprint)
            if recorded is not None:
                return recorded
            if request.deadline_ms <= self._now_ms():
                result = _refused(request, ERROR_DEADLINE_EXCEEDED)
                self._record(owner, key, fingerprint, result)
                return result
            registry = await self._registry(owner_id, owner)
            commands = request.commands
            if request.scene_id is not None:
                scene = registry.scene(request.scene_id)
                if scene is None:
                    result = _refused(request, ERROR_UNKNOWN_SCENE)
                    self._record(owner, key, fingerprint, result)
                    return result
                commands = scene.actions
            before: dict[str, dict[str, Any] | None] = {}
            results = []
            for command in commands:
                results.append(
                    await self._run(owner_id, owner, registry, command, request.deadline_ms, before)
                )
            result = ExecuteResult(request_id=request.request_id, results=tuple(results))
            # Recorded before anyone is told, so a failed send can never cause a rerun.
            self._record(owner, key, fingerprint, result)
            return result

    async def _run(
        self,
        owner_id: str,
        owner: _Owner,
        registry: Registry,
        command: Command,
        deadline_ms: int,
        before: dict[str, dict[str, Any] | None],
    ) -> CommandResult:
        remaining_ms = deadline_ms - self._now_ms()
        if remaining_ms <= 0:
            # Whoever asked has stopped waiting, and this one was never started.
            return _failed(command, ERROR_DEADLINE_EXCEEDED)
        # Another Owner's device is simply not in this registry.
        device = registry.device(command.device_id)
        if device is None:
            return _failed(command, ERROR_UNKNOWN_DEVICE)
        try:
            validate_command(device.type, command)
        except SmartHomeError as exc:
            return _failed(command, exc.code)
        provider = self._providers.get(device.provider)
        if provider is None:
            return _failed(command, ERROR_DEVICE_OFFLINE)
        try:
            async with asyncio.timeout(remaining_ms / 1000):
                state = await provider.execute(owner_id, device, command)
            validate_state(device.type, state)
        except SmartHomeError as exc:
            return _failed(command, exc.code)
        except TimeoutError:
            # The Provider may still act on it. Reconcile, never resend.
            return _unknown(command, ERROR_DEADLINE_EXCEEDED)
        except Exception:
            logger.exception(
                "smart home provider=%s owner=%s device=%s left the command unresolved",
                device.provider,
                owner_id,
                device.device_id,
            )
            return _unknown(command, None)
        before.setdefault(device.device_id, owner.states.get(device.device_id))
        owner.states[device.device_id] = state
        return CommandResult(device_id=command.device_id, status="succeeded", state=state)

    async def snapshot(self, owner_id: str) -> dict[str, Any]:
        """Current registry and Provider state for a trusted Agent command."""
        owner = self._owner(owner_id)
        async with owner.lock:
            registry = await self._registry(owner_id, owner)
            return {
                "registry": registry.model_dump(mode="json"),
                "status": {
                    device.device_id: {
                        "online": device.device_id in owner.states,
                        "state": owner.states.get(device.device_id, {}),
                    }
                    for device in registry.devices
                },
            }

    def _owner(self, owner_id: str) -> _Owner:
        if not owner_id:
            raise ValueError("owner_id is required")
        return self._owners.setdefault(owner_id, _Owner())

    async def _registry(self, owner_id: str, owner: _Owner) -> Registry:
        registry = await self._registry_source.get(owner_id)
        states = {}
        changed = owner.registry is None or owner.registry.revision != registry.revision
        for name, provider in self._providers.items():
            devices = [d for d in registry.devices if d.provider == name]
            if changed:
                await provider.reconcile(owner_id, devices)
            states.update(await provider.states(owner_id, devices))
        owner.registry, owner.states = registry, states
        return registry

    def _lookup(
        self, owner: _Owner, key: tuple[str, str], fingerprint: str
    ) -> ExecuteResult | None:
        now_ms = self._now_ms()
        # Constant TTL: insertion order is expiry order.
        while owner.recorded and next(iter(owner.recorded.values())).expires_at_ms <= now_ms:
            owner.recorded.popitem(last=False)
        recorded = owner.recorded.get(key)
        if recorded is None:
            return None
        if recorded.fingerprint != fingerprint:
            raise IdempotencyConflict(f"request_id {key[1]!r} was used for a different request")
        return recorded.result

    def _record(
        self, owner: _Owner, key: tuple[str, str], fingerprint: str, result: ExecuteResult
    ) -> None:
        owner.recorded[key] = _Recorded(self._now_ms() + self._ttl_ms, fingerprint, result)
        while len(owner.recorded) > self._capacity:
            owner.recorded.popitem(last=False)


def _fingerprint(request: ExecuteRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json", include={"commands", "scene_id"}),
        sort_keys=True,
        separators=(",", ":"),
    )


def _refused(request: ExecuteRequest, code: str) -> ExecuteResult:
    return ExecuteResult(request_id=request.request_id, error=code)


def _failed(command: Command, code: str) -> CommandResult:
    return CommandResult(device_id=command.device_id, status="failed", code=code)


def _unknown(command: Command, code: str | None) -> CommandResult:
    return CommandResult(device_id=command.device_id, status="unknown", code=code)
