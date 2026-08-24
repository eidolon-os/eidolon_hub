"""On-demand provision and independent durable revoke reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta

from hub.contracts.bindings.device import DeviceManifest, DeviceRef
from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.identity import Clock

from .domain import ChannelBinding, ChannelProviderError
from .ports import (
    ChannelBindingProvider,
    ChannelDeviceProjectionReader,
    ChannelRevocationStore,
)

_LOG = logging.getLogger(__name__)


def _operation_id(prefix: str, *values: object) -> str:
    encoded = "\0".join(str(value) for value in values).encode()
    return f"{prefix}-" + hashlib.sha256(encoded).hexdigest()[:48]


class ReconcileChannelBinding:
    """Return a Provider binding or an empty tuple while convergence is pending."""

    def __init__(
        self,
        *,
        devices: ChannelDeviceProjectionReader,
        provider: ChannelBindingProvider,
        clock: Clock,
    ) -> None:
        self._devices = devices
        self._provider = provider
        self._clock = clock

    async def execute(self, *, device_ref: DeviceRef) -> tuple[ChannelBinding, ...]:
        device = await self._devices.get(device_ref.device_instance_id)
        if (
            device is None
            or device.device_ref != device_ref
            or device.lifecycle_state is not DeviceLifecycleState.APPROVED
            or device.owner_id is None
        ):
            return ()
        manifest = DeviceManifest.model_validate_json(device.manifest_json)
        provision_id = _operation_id(
            "channel-provision",
            device_ref.model_dump_json(),
            device.manifest_revision,
        )
        values = {
            "device_ref": device_ref,
            "owner_id": device.owner_id,
            "display_name": device.display_name,
            "device_kind": device.device_kind,
            "manifest": manifest,
            "manifest_revision": device.manifest_revision,
        }
        try:
            channels = await self._provider.provision(operation_id=provision_id, **values)
            expires_at_ms = channels[0].expires_at_ms
            now_ms = int(self._clock.now().timestamp() * 1000)
            if expires_at_ms > now_ms:
                return channels
            refresh_id = _operation_id("channel-refresh", provision_id, expires_at_ms)
            refreshed = await self._provider.refresh(operation_id=refresh_id, **values)
            if refreshed[0].expires_at_ms <= now_ms:
                raise ChannelProviderError(
                    "EXPIRED_PROVIDER_BINDING",
                    retryable=True,
                    detail="Provider refresh returned expired credentials",
                )
            return refreshed
        except (ChannelProviderError, ValueError, IndexError, KeyError):
            _LOG.warning(
                "Channel binding pending device=%s generation=%s/%s/%s",
                device_ref.device_instance_id,
                device_ref.owner_domain_generation,
                device_ref.claim_generation,
                device_ref.trust_epoch,
                exc_info=True,
            )
            return ()


class ReconcileChannelRevocations:
    """Deliver ClaimRevoked effects without participating in Claim mutation."""

    def __init__(
        self,
        *,
        store: ChannelRevocationStore,
        provider: ChannelBindingProvider,
        clock: Clock,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._provider = provider
        self._clock = clock
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds

    async def execute(self, *, limit: int = 50) -> int:
        now = self._clock.now()
        await self._store.materialize_claim_events(now=now)
        converged = 0
        for operation in await self._store.list_due(now=now, limit=limit):
            try:
                await self._provider.revoke(
                    operation_id=operation.operation_id,
                    device_ref=operation.device_ref,
                    reason=operation.reason,
                )
            except ChannelProviderError as exc:
                if exc.code == "STALE_GENERATION":
                    await self._store.mark_terminal(
                        source_event_id=operation.source_event_id,
                        state="fenced",
                        result_code=exc.code,
                        delivered_at=self._clock.now(),
                    )
                    converged += 1
                    continue
                await self._retry(operation, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001 - infrastructure retry boundary
                await self._retry(operation, type(exc).__name__)
                continue
            await self._store.mark_terminal(
                source_event_id=operation.source_event_id,
                state="delivered",
                result_code="REVOKED",
                delivered_at=self._clock.now(),
            )
            converged += 1
        return converged

    async def _retry(self, operation, error: str) -> None:
        attempt = operation.attempt_count + 1
        delay = min(self._retry_max, self._retry_base * (2 ** min(attempt - 1, 10)))
        await self._store.mark_retry(
            source_event_id=operation.source_event_id,
            attempt_count=attempt,
            next_attempt_at=self._clock.now() + timedelta(seconds=delay),
            error=error,
        )
        _LOG.warning(
            "Channel revoke deferred event=%s attempt=%d error=%s",
            operation.source_event_id,
            attempt,
            error,
        )


class PeriodicChannelRevocationReconcile:
    def __init__(self, reconcile: ReconcileChannelRevocations, *, interval_seconds: float = 1.0):
        self._reconcile = reconcile
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="eidolon-channel-reconcile")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._reconcile.execute()
            except Exception:  # noqa: BLE001 - durable worker retries the next pass
                _LOG.exception("Channel reconciliation pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
