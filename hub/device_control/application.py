"""Use cases for operational-key delivery and signed ACK handling."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import timedelta

from hub.contracts.bindings.device import (
    AssertDeviceManifest,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceManifestAcceptance,
    DeviceRef,
    ManifestRef,
    canonical_bytes,
    verify_device_erase_ack,
    verify_p256_signature,
)
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.identity import Clock, IdGenerator
from hub.ports.management_events import DeviceManagementEventRecord
from hub.ports.repositories import DeviceMutationUnitOfWork, DeviceRepository

from .domain import (
    DeviceEraseGenerationConflict,
    DeviceEraseOperation,
    ManifestRevisionConflict,
)
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


@dataclass(frozen=True, slots=True)
class DeviceConfiguration:
    """What a device is told about itself when it asks.

    The accepted Manifest reference is part of a device's configuration, not a
    separate query: it is how a device learns which of its own declarations the
    Authority currently holds, and therefore whether it has anything to correct.
    Without it a device would have to guess the revision to assert at, and a
    wrong guess is refused — which is exactly the trap a device with a stale
    Manifest was already in.
    """

    claim: DeviceClaimProjection
    manifest: ManifestRef | None


class PullDeviceConfiguration:
    """Return one exact Claim projection without provisioning a business Channel."""

    def __init__(
        self,
        *,
        claims: DeviceClaimProjectionReader,
        devices: DeviceRepository,
    ) -> None:
        self._claims = claims
        self._devices = devices

    async def execute(
        self,
        *,
        device_ref: DeviceRef,
        public_key_spki: str,
        nonce: str,
        signature: str,
    ) -> DeviceConfiguration:
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
        device = await self._devices.get(device_ref.device_instance_id)
        manifest = (
            None
            if device is None or device.device_ref != device_ref
            else ManifestRef(
                manifest_id=device.device_kind,
                revision=device.manifest_declared_revision,
                digest=device.manifest_digest,
            )
        )
        return DeviceConfiguration(claim=claim, manifest=manifest)


class AcceptDeviceManifest:
    """Record what a claimed device now says it can do.

    The Owner approved an identity. This is that identity's own account of its
    capabilities, which changes whenever its firmware does, and which no other
    party is in a position to know. Admission is untouched: nothing here can
    alter who the device is, which generation it belongs to, or whether its
    Claim stands.
    """

    MANIFEST_ACCEPTED = "live.eidolon.device.manifest-accepted.v1"

    def __init__(
        self,
        *,
        claims: DeviceClaimProjectionReader,
        devices: DeviceRepository,
        mutations: DeviceMutationUnitOfWork,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._claims = claims
        self._devices = devices
        self._mutations = mutations
        self._ids = ids
        self._clock = clock

    async def execute(self, *, assertion: AssertDeviceManifest) -> DeviceManifestAcceptance:
        device_ref = assertion.device_ref
        claim = await self._claims.get_exact(device_ref=device_ref)
        if claim is None or claim.state != "active":
            raise KeyError(device_ref.device_instance_id)
        if not _same_operational_key(claim.operational_public_key_spki, assertion.public_key_spki):
            raise PermissionError("manifest assertion key differs from the Claim")
        verify_p256_signature(
            public_key_spki=assertion.public_key_spki,
            signing_document=assertion.signing_document(),
            signature=assertion.device_signature,
        )

        current = await self._devices.get(device_ref.device_instance_id)
        if current is None or current.device_ref != device_ref:
            raise KeyError(device_ref.device_instance_id)

        declared = assertion.manifest
        accepted = DeviceManifestDocument.from_declaration(
            document=declared.document, declared_revision=declared.revision
        )
        if declared.revision < current.manifest_declared_revision:
            raise ManifestRevisionConflict(
                "manifest revision is older than the accepted declaration"
            )
        if declared.revision == current.manifest_declared_revision:
            if accepted.digest != current.manifest_digest:
                raise ManifestRevisionConflict("manifest revision was reused for different content")
            # A device asserts on every boot; agreeing is the common case.
            return self._acceptance(assertion, outcome="unchanged")

        now = self._clock.now()
        await self._mutations.commit(
            expected=current,
            device=self._with_manifest(current, accepted, declared.manifest_id, now),
            event=DeviceManagementEventRecord(
                event_id=self._ids.new("manifest-acceptance"),
                event_type=self.MANIFEST_ACCEPTED,
                source="urn:eidolon:authority:device-control",
                principal_id=device_ref.device_instance_id,
                subject=device_ref.device_instance_id,
                occurred_at=now,
                data={
                    "manifest_id": declared.manifest_id,
                    "revision": declared.revision,
                    "digest": accepted.digest,
                    "superseded_digest": current.manifest_digest,
                },
            ),
        )
        return self._acceptance(assertion, outcome="accepted")

    @staticmethod
    def _with_manifest(
        device: ManagedDevice,
        manifest: DeviceManifestDocument,
        manifest_id: str,
        now,
    ) -> ManagedDevice:
        return replace(
            device,
            manifest=manifest,
            device_kind=manifest_id,
            updated_at=now,
            aggregate_revision=device.aggregate_revision + 1,
        )

    def _acceptance(
        self, assertion: AssertDeviceManifest, *, outcome: str
    ) -> DeviceManifestAcceptance:
        return DeviceManifestAcceptance(
            device_ref=assertion.device_ref,
            nonce=assertion.nonce,
            accepted=assertion.manifest.ref,
            outcome=outcome,
            accepted_at=self._clock.now(),
        )


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
