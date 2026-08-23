"""Use cases for operation key binding, delivery and signed ACK handling."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.contracts.bindings.device import (
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceOperationKeyProof,
    DeviceRef,
    canonical_bytes,
    operation_key_id,
    verify_device_erase_ack,
    verify_operation_key_proof,
    verify_p256_signature,
)
from hub.domain.channels.entities import ChannelAssignmentSet
from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.identity import Clock, RetrievalTokenHasher
from hub.ports.repositories import DeviceRepository

from .domain import DeviceEraseGenerationConflict, DeviceEraseOperation
from .ports import DeviceEraseLedger

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceEraseDelivery:
    delivery_attempt_id: str
    command: DeviceLocalEraseCommand


class BindDeviceOperationKey:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        tokens: RetrievalTokenHasher,
        ledger: DeviceEraseLedger,
        clock: Clock,
    ) -> None:
        self._devices = devices
        self._tokens = tokens
        self._ledger = ledger
        self._clock = clock

    async def execute(
        self,
        *,
        enrollment_id: str,
        retrieval_token: str,
        proof: DeviceOperationKeyProof,
    ) -> str:
        verify_operation_key_proof(proof)
        device = await self._devices.get_by_enrollment_id(enrollment_id)
        if device is None:
            raise KeyError(enrollment_id)
        if self._clock.now() >= device.retrieval_expires_at:
            raise TimeoutError("operation key binding window expired")
        if not self._tokens.verify(retrieval_token, device.retrieval_token_hash):
            raise PermissionError("invalid enrollment retrieval capability")
        if (
            proof.device_instance_id != device.identity.device_id
            or proof.enrollment_request_id != device.last_enrollment_request_id
        ):
            raise PermissionError("operation key proof is not bound to this enrollment")
        if device.lifecycle_state not in {
            DeviceLifecycleState.PENDING_APPROVAL,
            DeviceLifecycleState.APPROVED,
        }:
            raise PermissionError("operation key cannot be rebound after Claim revocation")
        key_id = operation_key_id(proof.public_key_spki)
        await self._ledger.bind_operation_key(
            enrollment_id=enrollment_id,
            owner_domain_generation=device.owner_domain_generation,
            claim_generation=device.claim_generation,
            proof=proof,
            key_id=key_id,
            bound_at=self._clock.now(),
        )
        return key_id


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
        created = await self._ledger.materialize_claim_events(
            now=now, operation_ttl=self._ttl
        )
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
        if operation.public_key_spki is None or public_key_spki != operation.public_key_spki:
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
        attempt_id = "erase_delivery_" + hashlib.sha256(
            f"{operation.command.operation_id}\0{nonce}".encode()
        ).hexdigest()[:40]
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


@dataclass(frozen=True, slots=True)
class DeviceConfigurationOutcome:
    device_ref: DeviceRef
    lifecycle_state: DeviceLifecycleState
    assignments: ChannelAssignmentSet | None


class PullDeviceConfiguration:
    """Serve an established Claim through Device Control, not Enrollment."""

    def __init__(
        self,
        *,
        devices: DeviceRepository,
        ledger: DeviceEraseLedger,
        provision: ProvisionDeviceChannels,
    ) -> None:
        self._devices = devices
        self._ledger = ledger
        self._provision = provision

    async def execute(
        self,
        *,
        device_ref: DeviceRef,
        public_key_spki: str,
        nonce: str,
        signature: str,
    ) -> DeviceConfigurationOutcome:
        device = await self._devices.get(device_ref.device_instance_id)
        if device is None or device.device_ref != device_ref:
            raise KeyError(device_ref.device_instance_id)
        binding = await self._ledger.operation_key_for(device_ref=device_ref)
        if binding is None or binding[0] != public_key_spki:
            raise PermissionError("configuration key does not match the Claim generation")
        verify_p256_signature(
            public_key_spki=public_key_spki,
            signing_document={
                "device_ref": device_ref.model_dump(mode="json"),
                "nonce": nonce,
                "operation_type": "device-control.configuration",
            },
            signature=signature,
        )
        assignments = None
        if device.lifecycle_state is DeviceLifecycleState.APPROVED:
            semantic = canonical_bytes(device_ref)
            operation_id = "claim-config_" + hashlib.sha256(semantic).hexdigest()[:48]
            assignments = await self._provision.execute(
                device=device,
                operation_id=operation_id,
            )
        return DeviceConfigurationOutcome(
            device_ref=device_ref,
            lifecycle_state=device.lifecycle_state,
            assignments=assignments,
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
