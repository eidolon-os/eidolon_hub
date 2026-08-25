"""Use cases for operational-key delivery and signed ACK handling."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from hub.contracts.bindings.device import (
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceRef,
    canonical_bytes,
    verify_device_erase_ack,
    verify_p256_signature,
)
from hub.ports.identity import Clock

from .domain import DeviceEraseGenerationConflict, DeviceEraseOperation
from .ports import DeviceClaimProjection, DeviceClaimProjectionReader, DeviceEraseLedger

_LOG = logging.getLogger(__name__)

#: The scheme Admission records an operational SPKI under. Device Control's own
#: contract spells the same key as bare base64url — its schema pattern forbids
#: the colon — so the two adjacent contracts hold one key in two spellings.
_SPKI_SCHEME = "p256-spki:"


def _same_operational_key(recorded: str | None, presented: str) -> bool:
    """Is the key a device presents the key its Claim recorded?

    Compared as a key, not as a string. Admission stores `p256-spki:<base64>`
    and Device Control receives `<base64>`, so a plain `!=` could never be
    false for a correct device — every claimed device was refused on the first
    call it makes after a reboot, and neither spelling could have passed.
    """

    if recorded is None:
        return False
    return recorded.removeprefix(_SPKI_SCHEME) == presented.removeprefix(_SPKI_SCHEME)


@dataclass(frozen=True, slots=True)
class DeviceEraseDelivery:
    delivery_attempt_id: str
    command: DeviceLocalEraseCommand


class PullDeviceConfiguration:
    """Return one exact Claim projection without provisioning a business Channel."""

    def __init__(self, *, claims: DeviceClaimProjectionReader) -> None:
        self._claims = claims

    async def execute(
        self,
        *,
        device_ref: DeviceRef,
        public_key_spki: str,
        nonce: str,
        signature: str,
    ) -> DeviceClaimProjection:
        claim = await self._claims.get_exact(device_ref=device_ref)
        if claim is None:
            raise KeyError(device_ref.device_instance_id)
        if not _same_operational_key(claim.operational_public_key_spki, public_key_spki):
            raise PermissionError("configuration key differs from the Claim")
        verify_p256_signature(
            public_key_spki=public_key_spki,
            signing_document={
                "device_ref": device_ref.model_dump(mode="json"),
                "nonce": nonce,
                "operation_type": "device-control.configuration",
            },
            signature=signature,
        )
        return claim


class ReconcileDeviceEraseOperations:
    def __init__(
        self,
        *,
        ledger: DeviceEraseLedger,
        clock: Clock,
        operation_ttl: timedelta,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._ttl = operation_ttl

    async def execute(self) -> int:
        now = self._clock.now()
        created = await self._ledger.materialize_claim_events(now=now, operation_ttl=self._ttl)
        await self._ledger.mark_accepted_pending(now=now)
        await self._ledger.expire_due(now=now)
        return created


class PullDeviceEraseOperation:
    def __init__(self, *, ledger: DeviceEraseLedger, clock: Clock) -> None:
        self._ledger = ledger
        self._clock = clock

    async def execute(
        self,
        *,
        device_ref: DeviceRef,
        public_key_spki: str,
        nonce: str,
        signature: str,
    ) -> DeviceEraseDelivery | None:
        operation = await self._ledger.get_for_device(device_ref=device_ref)
        if operation is None:
            return None
        if not _same_operational_key(operation.public_key_spki, public_key_spki):
            raise PermissionError("operation delivery key does not match the Claim generation")
        signing_document = {
            "device_ref": device_ref.model_dump(mode="json"),
            "nonce": nonce,
            "operation_type": "device-local.erase",
        }
        verify_p256_signature(
            public_key_spki=public_key_spki,
            signing_document=signing_document,
            signature=signature,
        )
        if operation.state.value in {"acknowledged", "expired", "permanent-failure"}:
            return None
        attempt_id = (
            "erase_delivery_"
            + hashlib.sha256(f"{operation.command.operation_id}\0{nonce}".encode()).hexdigest()[:40]
        )
        accepted = await self._ledger.accept_delivery(
            operation_id=operation.command.operation_id,
            delivery_attempt_id=attempt_id,
            accepted_at=self._clock.now(),
        )
        if accepted.state.value in {"acknowledged", "expired", "permanent-failure"}:
            return None
        return DeviceEraseDelivery(
            delivery_attempt_id=attempt_id,
            command=accepted.command,
        )


class AcknowledgeDeviceEraseOperation:
    def __init__(self, *, ledger: DeviceEraseLedger, clock: Clock) -> None:
        self._ledger = ledger
        self._clock = clock

    async def execute(self, *, ack: DeviceLocalEraseAck) -> DeviceEraseOperation:
        operation = await self._ledger.get(operation_id=ack.operation_id)
        if operation is None:
            raise KeyError(ack.operation_id)
        if ack.device_ref != operation.command.device_ref:
            raise DeviceEraseGenerationConflict(
                "ACK DeviceRef does not match the operation Claim generation"
            )
        if operation.public_key_spki is None:
            raise PermissionError("operation has no bound device ACK key")
        verify_device_erase_ack(
            ack=ack,
            public_key_spki=operation.public_key_spki,
        )
        fingerprint = "sha256:" + hashlib.sha256(canonical_bytes(ack)).hexdigest()
        return await self._ledger.apply_ack(
            ack=ack,
            ack_fingerprint=fingerprint,
            received_at=self._clock.now(),
        )


class PeriodicDeviceEraseReconcile:
    def __init__(self, reconcile: ReconcileDeviceEraseOperations, *, interval_seconds: float):
        self._reconcile = reconcile
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="eidolon-device-erase-reconcile")

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
            except Exception:  # noqa: BLE001 - durable worker retries next pass
                _LOG.exception("device-local.erase reconcile pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
