"""Owner-scoped device execution. No transport, panel or model dependencies.

What this runtime guarantees, independent of the Provider behind a device:

- a request is idempotent by (scope, request_id) and that record survives a
  restart (``ReceiptLedger``); a reuse for different content is a conflict;
- one Owner's requests are admitted one at a time, but Provider I/O runs
  outside the Owner lock, serialised per device only, so a slow device never
  holds up another;
- what the panel and the Agent read is observed state (``ObservationCache``),
  never what a command intended;
- a platform that only takes instructions in words yields ``delegated``, which
  this runtime never upgrades to ``succeeded``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
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
    Device,
    ExecuteRequest,
    ExecuteResult,
    Registry,
    SmartHomeError,
    provider_binding,
    validate_device_command,
    validate_device_state,
)

from hub.integration.ledger import MemoryLedger, ReceiptConflict, ReceiptLedger, Timestamps
from hub.integration.observation import MemoryObservationCache, ObservationCache

from .effects import satisfied
from .ports import Delegated, RegistrySource, SmartHomeProvider

logger = logging.getLogger("hub.smarthome")
# How long an unknown outcome waits for an observation that settles it.
RECONCILE_WINDOW_MS = 10 * 60_000


class IdempotencyConflict(ValueError):
    """A request_id was reused for different commands or a different scene.

    Answering with the first result would report commands that never ran.
    """


@dataclass(frozen=True, slots=True)
class _Pending:
    """An unknown outcome waiting for the observation that settles it."""

    scope: str
    request_id: str
    command: Command
    before: dict[str, Any] | None
    expires_at_ms: int


@dataclass
class _Owner:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    registry: Registry | None = None
    pending: dict[str, list[_Pending]] = field(default_factory=dict)
    inflight: dict[tuple[str, str], asyncio.Future[ExecuteResult]] = field(default_factory=dict)
    device_locks: defaultdict[str, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )


@dataclass
class _Timing:
    submitted_at_ms: int
    provider_started_at_ms: int | None = None
    provider_returned_at_ms: int | None = None
    confirmed_at_ms: int | None = None


class SmartHomeRuntime:
    def __init__(
        self,
        *,
        registry: RegistrySource,
        providers: Mapping[str, SmartHomeProvider],
        now_ms: Callable[[], int] | None = None,
        ledger: ReceiptLedger | None = None,
        observations: ObservationCache | None = None,
    ):
        self._registry_source = registry
        self._providers = dict(providers)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._ledger = ledger if ledger is not None else MemoryLedger()
        self._observations = observations if observations is not None else MemoryObservationCache()
        self._owners: dict[str, _Owner] = {}

    @property
    def observations(self) -> ObservationCache:
        return self._observations

    @property
    def ledger(self) -> ReceiptLedger:
        return self._ledger

    @property
    def providers(self) -> Mapping[str, SmartHomeProvider]:
        return self._providers

    async def execute(self, owner_id: str, request: ExecuteRequest) -> ExecuteResult:
        scope = request.origin.device_ref if request.origin.kind == "touch" else ""
        return await self._execute(owner_id, request, scope=scope or "")

    async def _execute(
        self, owner_id: str, request: ExecuteRequest, *, scope: str
    ) -> ExecuteResult:
        key = (scope, request.request_id)
        fingerprint = _fingerprint(request)
        owner = self._owner(owner_id)
        waiting: asyncio.Future[ExecuteResult] | None = None
        async with owner.lock:
            try:
                existing = await self._ledger.begin(
                    owner_id, scope, request.request_id, fingerprint, self._now_ms()
                )
            except ReceiptConflict as exc:
                raise IdempotencyConflict(
                    f"request_id {request.request_id!r} was used for a different request"
                ) from exc
            if existing is not None:
                if existing.result is not None:
                    return ExecuteResult.model_validate(existing.result)
                waiting = owner.inflight.get(key)
            if waiting is None:
                # Either new, or recorded in flight by a process that died before
                # completing; in both cases this call carries it out.
                owner.inflight[key] = asyncio.get_running_loop().create_future()
        if waiting is not None:
            return await asyncio.shield(waiting)
        future = owner.inflight[key]
        timing = _Timing(submitted_at_ms=self._now_ms())
        try:
            result = await self._perform(owner_id, owner, request, timing, scope)
            # Recorded before anyone is told, so a failed send can never cause a rerun.
            await self._ledger.complete(
                owner_id,
                scope,
                request.request_id,
                result.model_dump(mode="json"),
                Timestamps(
                    timing.submitted_at_ms,
                    timing.provider_started_at_ms,
                    timing.provider_returned_at_ms,
                    timing.confirmed_at_ms,
                    self._now_ms(),
                ),
            )
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            owner.inflight.pop(key, None)
        if not future.done():
            future.set_result(result)
        return result

    async def _perform(
        self,
        owner_id: str,
        owner: _Owner,
        request: ExecuteRequest,
        timing: _Timing,
        scope: str = "",
    ) -> ExecuteResult:
        if request.deadline_ms <= self._now_ms():
            return _refused(request, ERROR_DEADLINE_EXCEEDED)
        async with owner.lock:
            registry = await self._registry(owner_id, owner)
        commands = request.commands
        if request.scene_id is not None:
            scene = registry.scene(request.scene_id)
            if scene is None:
                return _refused(request, ERROR_UNKNOWN_SCENE)
            commands = scene.actions
        timing.provider_started_at_ms = self._now_ms()
        results = await asyncio.gather(
            *(
                self._run(
                    owner_id,
                    owner,
                    registry,
                    command,
                    request.deadline_ms,
                    scope,
                    request.request_id,
                )
                for command in commands
            )
        )
        timing.provider_returned_at_ms = self._now_ms()
        if any(r.status == "succeeded" for r in results):
            timing.confirmed_at_ms = timing.provider_returned_at_ms
        return ExecuteResult(request_id=request.request_id, results=tuple(results))

    async def _run(
        self,
        owner_id: str,
        owner: _Owner,
        registry: Registry,
        command: Command,
        deadline_ms: int,
        scope: str = "",
        request_id: str = "",
    ) -> CommandResult:
        # Another Owner's device is simply not in this registry.
        device = registry.device(command.device_id)
        if device is None:
            return _failed(command, ERROR_UNKNOWN_DEVICE)
        try:
            validate_device_command(device, command)
        except SmartHomeError as exc:
            return _failed(command, exc.code)
        kind, _account = provider_binding(device.provider)
        provider = self._providers.get(kind)
        if provider is None or device.orphaned:
            return _failed(command, ERROR_DEVICE_OFFLINE)
        async with owner.device_locks[device.device_id]:
            remaining_ms = deadline_ms - self._now_ms()
            if remaining_ms <= 0:
                # Whoever asked has stopped waiting, and this one was never started.
                return _failed(command, ERROR_DEADLINE_EXCEEDED)
            try:
                async with asyncio.timeout(remaining_ms / 1000):
                    outcome = await provider.execute(owner_id, device, command)
                if not isinstance(outcome, Delegated):
                    validate_device_state(device, outcome)
            except SmartHomeError as exc:
                return _failed(command, exc.code)
            except TimeoutError:
                # The Provider may still act on it. Reconcile, never resend.
                seen = (await self._observations.snapshot(owner_id)).get(device.device_id)
                owner.pending.setdefault(device.device_id, []).append(
                    _Pending(
                        scope,
                        request_id,
                        command,
                        None if seen is None else seen.state,
                        self._now_ms() + RECONCILE_WINDOW_MS,
                    )
                )
                return _unknown(command, ERROR_DEADLINE_EXCEEDED)
            except Exception:
                logger.exception(
                    "smart home provider=%s owner=%s device=%s left the command unresolved",
                    device.provider,
                    owner_id,
                    device.device_id,
                )
                return _unknown(command, None)
        if isinstance(outcome, Delegated):
            await self._observed(owner_id, owner, device.device_id, reachable=True, state=None)
            return CommandResult(
                device_id=command.device_id,
                status="delegated",
                platform_answer=outcome.answer[:200],
            )
        await self._observed(owner_id, owner, device.device_id, reachable=True, state=outcome)
        return CommandResult(device_id=command.device_id, status="succeeded", state=outcome)

    async def snapshot(self, owner_id: str) -> dict[str, Any]:
        """Current registry and observed state for a trusted Agent command."""
        owner = self._owner(owner_id)
        async with owner.lock:
            registry = await self._registry(owner_id, owner)
        observed = await self._observations.snapshot(owner_id)
        status = {}
        for device in registry.devices:
            seen = observed.get(device.device_id)
            online = seen is not None and seen.reachable and not device.orphaned
            status[device.device_id] = {
                "online": online,
                "state": seen.state if seen is not None and seen.state is not None else {},
            }
        return {"registry": registry.model_dump(mode="json"), "status": status}

    async def observe(
        self, owner_id: str, device_id: str, *, reachable: bool, state: dict[str, Any] | None
    ) -> None:
        """An adapter reports what it saw; the cache decides whether it is a change."""
        await self._observed(
            owner_id, self._owner(owner_id), device_id, reachable=reachable, state=state
        )

    async def _observed(
        self,
        owner_id: str,
        owner: _Owner,
        device_id: str,
        *,
        reachable: bool,
        state: dict[str, Any] | None,
    ) -> None:
        """Record an observation and settle any unknown outcome it answers."""
        now = self._now_ms()
        await self._observations.put(
            owner_id, device_id, reachable=reachable, state=state, observed_at_ms=now
        )
        waiting = owner.pending.get(device_id)
        if not waiting:
            return
        remaining: list[_Pending] = []
        for item in waiting:
            if item.expires_at_ms <= now:
                continue
            if state is not None and satisfied(item.command, item.before, state):
                settled = CommandResult(device_id=device_id, status="succeeded", state=state)
                await self._ledger.reconcile(
                    owner_id,
                    item.scope,
                    item.request_id,
                    device_id,
                    settled.model_dump(mode="json"),
                    now,
                )
                logger.info(
                    "smart home reconciled owner=%s device=%s request=%s",
                    owner_id,
                    device_id,
                    item.request_id,
                )
                continue
            remaining.append(item)
        if remaining:
            owner.pending[device_id] = remaining
        else:
            owner.pending.pop(device_id, None)

    def _owner(self, owner_id: str) -> _Owner:
        if not owner_id:
            raise ValueError("owner_id is required")
        return self._owners.setdefault(owner_id, _Owner())

    async def _registry(self, owner_id: str, owner: _Owner) -> Registry:
        registry = await self._registry_source.get(owner_id)
        changed = owner.registry is None or owner.registry.revision != registry.revision
        for kind, provider in self._providers.items():
            devices = [d for d in devices_of(registry, kind) if not d.orphaned]
            if changed:
                await provider.reconcile(owner_id, devices)
            if getattr(provider, "pushes_observations", False):
                continue
            states = await provider.states(owner_id, devices)
            for device in devices:
                state = states.get(device.device_id)
                await self._observed(
                    owner_id, owner, device.device_id, reachable=state is not None, state=state
                )
        if changed and owner.registry is not None:
            gone = {d.device_id for d in owner.registry.devices} - {
                d.device_id for d in registry.devices
            }
            await self._observations.forget(owner_id, gone)
        owner.registry = registry
        return registry


def devices_of(registry: Registry, kind: str) -> list[Device]:
    return [d for d in registry.devices if provider_binding(d.provider)[0] == kind]


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
