"""On-demand provision and independent durable revoke reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta

from hub.contracts.bindings.device import DeviceRef
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
        # Forwarded verbatim, as the Provider's own contract says it is: the
        # accepted Manifest is the device's document, and this Authority does
        # not speak its vocabulary. Parsing it into Hub's own affordance model
        # meant a device whose Manifest was simply shaped differently — a real
        # BOX-3 sends `{"endpoints": []}` — made this raise, above the guard
        # that exists to answer "binding pending", so the configuration pull
        # answered 500 to a device that was correctly claimed.
        try:
            manifest = json.loads(device.manifest_json)
        except json.JSONDecodeError:
            manifest = None
        if not isinstance(manifest, dict):
            # Not pending. A stored document that is not an object does not
            # become one by waiting, and the only thing that can change it is
            # the device asserting a new Manifest.
            _LOG.error(
                "Channel binding refused: accepted Manifest is not an object device=%s",
                device_ref.device_instance_id,
            )
            return ()
        provision_id = _operation_id(
            "channel-provision",
            device_ref.model_dump_json(),
            device.manifest_digest,
        )
        values = {
            "device_ref": device_ref,
            "owner_id": device.owner_id,
            "display_name": device.display_name,
            "manifest_id": device.manifest_id,
            "manifest": manifest,
            # The Provider keys its binding on this; a re-asserted Manifest
            # therefore invalidates the binding by changing the digest.
            "manifest_revision": device.manifest_digest,
        }
        # Read before deciding. ``provision`` begins a generation and
        # ``refresh`` advances one, and only the Provider knows which is
        # needed. Issuing ``provision`` every time and refreshing if its answer
        # looked expired worked exactly once: the first refresh fences the
        # provision row, so the next reconcile replayed a spent idempotency key
        # and was refused as "a terminal fenced lifecycle" — permanently. Every
        # device lost its channel about two hours after enrolment and could
        # never get one again, while the Claim, the mount and the Companion
        # binding all still read healthy.
        try:
            now_ms = int(self._clock.now().timestamp() * 1000)
            current = await self._provider.current(device_ref=device_ref)
            if current is None or current.manifest_revision != device.manifest_digest:
                # No binding, or one established for a Manifest this device no
                # longer asserts. Either way this begins a generation, and the
                # Manifest digest in the id keeps a re-asserted Manifest from
                # reusing the previous one's key.
                return _unexpired(
                    await self._provider.provision(operation_id=provision_id, **values),
                    now_ms,
                )
            if current.expires_at_ms > now_ms:
                return current.channels
            refresh_id = _operation_id(
                "channel-refresh", current.operation_id, current.expires_at_ms
            )
            return _unexpired(
                await self._provider.refresh(operation_id=refresh_id, **values), now_ms
            )
        # "Pending" is a claim about the future: keep asking and this
        # converges. A Provider that refused the request against its own
        # contract will refuse the identical request forever, and there is
        # nothing left for the device to wait for. Both used to be recorded as
        # pending, so a Manifest the Provider cannot read was indistinguishable
        # from a Provider that was briefly down — for a device whose Claim,
        # mount and Companion binding all read healthy, and whose owner had
        # already spent the one irrevocable approval.
        #
        # What the device is answered does not change: this Authority does not
        # fail a configuration pull because the Channel is not ready. Answering
        # 500 there is the other half of the same incident, and it is why the
        # guard exists at all. Only the record of why it is empty changes.
        except ChannelProviderError as exc:
            if exc.retryable:
                _LOG.warning(
                    "Channel binding pending device=%s generation=%s/%s/%s code=%s",
                    device_ref.device_instance_id,
                    device_ref.owner_domain_generation,
                    device_ref.claim_generation,
                    device_ref.trust_epoch,
                    exc.code,
                    exc_info=True,
                )
            else:
                _LOG.error(
                    "Channel binding refused, and waiting will not change it: "
                    "device=%s generation=%s/%s/%s code=%s",
                    device_ref.device_instance_id,
                    device_ref.owner_domain_generation,
                    device_ref.claim_generation,
                    device_ref.trust_epoch,
                    exc.code,
                    exc_info=True,
                )
            return ()
        except (ValueError, IndexError, KeyError):
            # Not a Provider verdict but a shape neither side declared. It is
            # deterministic, so it is not pending either.
            _LOG.error(
                "Channel binding refused: unreadable Provider exchange "
                "device=%s generation=%s/%s/%s",
                device_ref.device_instance_id,
                device_ref.owner_domain_generation,
                device_ref.claim_generation,
                device_ref.trust_epoch,
                exc_info=True,
            )
            return ()


def _unexpired(channels: tuple[ChannelBinding, ...], now_ms: int) -> tuple[ChannelBinding, ...]:
    if channels[0].expires_at_ms <= now_ms:
        raise ChannelProviderError(
            "EXPIRED_PROVIDER_BINDING",
            retryable=True,
            detail="Provider returned expired credentials",
        )
    return channels


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
