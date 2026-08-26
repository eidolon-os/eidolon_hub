"""HTTPS DeviceDeliveryPort adapter for signed erase pull and ACK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hub.channel_reconciliation.application import ReconcileChannelBinding
from hub.channel_reconciliation.domain import ChannelBinding
from hub.contracts.bindings.admission import (
    AdmissionCredentialError,
    read_admission_credential,
)
from hub.contracts.bindings.device import (
    AssertDeviceManifest,
    DeliverEnvelope,
    DeliveryAcceptance,
    DeviceEraseContractError,
    DeviceEvidenceEnvelope,
    DeviceLocalEraseAck,
    DeviceLocalEraseOperationStatus,
    DeviceManifestAcceptance,
    DeviceRef,
    ManifestRef,
)

from .application import (
    AcceptDeviceManifest,
    AcknowledgeDeviceEraseOperation,
    PullDeviceConfiguration,
    PullDeviceEraseOperation,
    ReconcileDeviceEraseOperations,
)
from .domain import (
    DeviceEraseGenerationConflict,
    DeviceEraseIdempotencyConflict,
    ManifestRevisionConflict,
)
from .ports import DeviceEraseLedger


class _AdapterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PullEraseOperationRequest(_AdapterModel):
    device_ref: DeviceRef
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    public_key_spki: str = Field(min_length=120, max_length=256)
    device_signature: str = Field(min_length=86, max_length=86)


class PullDeviceConfigurationRequest(PullEraseOperationRequest):
    pass


class DeviceConfigurationResult(_AdapterModel):
    operation: str = "device-control.configuration"
    nonce: str
    device_ref: DeviceRef
    lifecycle_state: str
    # Which of this device's own declarations the Authority currently holds, so
    # a device with something to correct knows what it is correcting.
    manifest: ManifestRef | None = None
    channels: tuple[ChannelBinding, ...] = Field(default=(), max_length=1)


@dataclass(frozen=True, slots=True)
class DeviceEraseHttpServices:
    configuration: PullDeviceConfiguration
    manifest: AcceptDeviceManifest
    channel_binding: ReconcileChannelBinding
    pull: PullDeviceEraseOperation
    acknowledge: AcknowledgeDeviceEraseOperation
    reconcile: ReconcileDeviceEraseOperations
    ledger: DeviceEraseLedger
    #: The installation secret this surface reads Admission credentials with.
    secret: bytes


def create_device_erase_router(
    services: DeviceEraseHttpServices | Callable[[], DeviceEraseHttpServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/device-control/v1", tags=["device-control"])

    def current() -> DeviceEraseHttpServices:
        return services() if callable(services) else services

    async def authorize_exact_status(
        *,
        authorization: str,
        device_ref: DeviceRef,
    ) -> None:
        """Fence this read to the exact device the credential authorizes.

        The same Admission credential the Claim revocation is presented with:
        one removal, one authorization, read by both surfaces. Each enforces
        what it needs — revocation checks the command's DeviceRef against the
        stored Claim, this checks the request against what the credential was
        minted for — but there is one credential and one vocabulary. Two
        vocabularies for one authorization is what answered 401 to every
        device removal ever attempted.

        A credential with no target is refused rather than treated as
        authorizing whatever arrived: "not fenced" is not "fenced to this".
        """

        try:
            credential = read_admission_credential(authorization, secret=current().secret)
        except AdmissionCredentialError as exc:
            raise PermissionError(str(exc)) from exc
        if "device.claim.revoke" not in credential.scopes:
            raise PermissionError("credential lacks device.claim.revoke scope")
        if credential.target_device_ref is None:
            raise PermissionError("credential authorizes no device")
        if credential.target_device_ref != device_ref:
            raise PermissionError("credential authorizes another device or generation")

    @router.post(
        "/configuration:pull",
        response_model=DeviceConfigurationResult,
    )
    async def pull_configuration(
        payload: PullDeviceConfigurationRequest,
    ) -> DeviceConfigurationResult:
        try:
            configuration = await current().configuration.execute(
                device_ref=payload.device_ref,
                public_key_spki=payload.public_key_spki,
                nonce=payload.nonce,
                signature=payload.device_signature,
            )
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="STALE_GENERATION") from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device proof rejected") from exc
        claim = configuration.claim
        if claim.state not in {"active", "revoked"}:
            raise HTTPException(status_code=409, detail="CLAIM_NOT_ACTIVE")
        channels = (
            await current().channel_binding.execute(device_ref=claim.device_ref)
            if claim.state == "active"
            else ()
        )
        return DeviceConfigurationResult(
            nonce=payload.nonce,
            device_ref=claim.device_ref,
            lifecycle_state="approved" if claim.state == "active" else "revoked",
            manifest=configuration.manifest,
            channels=channels,
        )

    @router.post(
        "/manifest:assert",
        response_model=DeviceManifestAcceptance,
    )
    async def assert_manifest(payload: AssertDeviceManifest) -> DeviceManifestAcceptance:
        """A claimed device's own, current account of what it can do."""

        try:
            return await current().manifest.execute(assertion=payload)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="CLAIM_NOT_ACTIVE") from exc
        except ManifestRevisionConflict as exc:
            raise HTTPException(status_code=409, detail="MANIFEST_REVISION_CONFLICT") from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device proof rejected") from exc

    @router.post(
        "/erase-operations:pull",
        # Deliberately not a response model. "There is nothing to deliver" is
        # the normal answer for every healthy claimed device, and it is a 204
        # with no body — which a declared model cannot express: FastAPI
        # validated `None` against it and answered 500 instead. That made the
        # first call a device makes after a reboot fail, so a claimed device
        # could not finish booting at all. The envelope is still constructed
        # here, so the contract is checked where it is produced.
        response_model=None,
        responses={
            200: {"model": DeliverEnvelope},
            204: {"description": "No non-terminal operation for this generation"},
        },
    )
    async def pull_operation(payload: PullEraseOperationRequest) -> Response:
        runtime = current()
        await runtime.reconcile.execute()
        try:
            delivery = await runtime.pull.execute(
                device_ref=payload.device_ref,
                public_key_spki=payload.public_key_spki,
                nonce=payload.nonce,
                signature=payload.device_signature,
            )
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device proof rejected") from exc
        if delivery is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        envelope = DeliverEnvelope(
            delivery_attempt_id=delivery.delivery_attempt_id,
            message_id=delivery.command.operation_id,
            kind="operation",
            device_ref=delivery.command.device_ref,
            deadline=delivery.command.deadline,
            payload_schema=(
                "https://contracts.eidolon.live/device-foundation/v1/"
                "device-control/schemas.schema.json#/$defs/DeviceLocalEraseCommand"
            ),
            payload=delivery.command.model_dump(mode="json"),
        )
        return JSONResponse(status_code=200, content=envelope.model_dump(mode="json"))

    @router.post(
        "/erase-operations/{operation_id}/ack",
        response_model=DeliveryAcceptance,
    )
    async def acknowledge_operation(
        operation_id: str, evidence: DeviceEvidenceEnvelope
    ) -> DeliveryAcceptance:
        if (
            evidence.message_id != operation_id
            or evidence.payload_schema
            != "https://contracts.eidolon.live/device-foundation/v1/device-control/"
            "schemas.schema.json#/$defs/DeviceLocalEraseAck"
        ):
            raise HTTPException(status_code=422, detail="path and ACK operation_id differ")
        try:
            payload = DeviceLocalEraseAck.model_validate(evidence.payload)
            if payload.operation_id != operation_id or payload.device_ref != evidence.device_ref:
                raise DeviceEraseGenerationConflict("evidence and nested ACK DeviceRef differ")
            current_operation = await current().ledger.get(operation_id=operation_id)
            if current_operation is None:
                raise KeyError(operation_id)
            if (
                current_operation.command.device_ref != evidence.device_ref
                or current_operation.delivery_attempt_id != evidence.delivery_attempt_id
            ):
                raise DeviceEraseGenerationConflict(
                    "evidence is not bound to the accepted Delivery attempt"
                )
            await current().acknowledge.execute(ack=payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation not found") from exc
        except DeviceEraseGenerationConflict as exc:
            raise HTTPException(status_code=409, detail="STALE_GENERATION") from exc
        except DeviceEraseIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device ACK signature rejected") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid device ACK evidence") from exc
        return DeliveryAcceptance(
            delivery_attempt_id=evidence.delivery_attempt_id,
            state="accepted",
        )

    @router.get(
        "/owners/{owner_scope}/devices/{device_id}/erase-operations",
        response_model=DeviceLocalEraseOperationStatus,
    )
    async def get_status_by_source_event(
        owner_scope: str,
        device_id: str,
        source_claim_event_id: str,
        owner_domain_generation: int,
        claim_generation: int,
        trust_epoch: int,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceLocalEraseOperationStatus:
        runtime = current()
        await runtime.reconcile.execute()
        try:
            device_ref = DeviceRef(
                device_instance_id=device_id,
                owner_domain_id=owner_scope,
                owner_domain_generation=owner_domain_generation,
                claim_generation=claim_generation,
                trust_epoch=trust_epoch,
            )
            await authorize_exact_status(
                authorization=authorization,
                device_ref=device_ref,
            )
            operation = await runtime.ledger.get_by_source_event(
                source_claim_event_id=source_claim_event_id,
                device_ref=device_ref,
            )
            if operation is None:
                raise KeyError(source_claim_event_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="forbidden") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid DeviceRef") from exc
        return operation.status

    @router.get(
        "/owners/{owner_scope}/devices/{device_id}/erase-operations/{operation_id}",
        response_model=DeviceLocalEraseOperationStatus,
    )
    async def get_status(
        owner_scope: str,
        device_id: str,
        operation_id: str,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceLocalEraseOperationStatus:
        runtime = current()
        await runtime.reconcile.execute()
        try:
            operation = await runtime.ledger.get(operation_id=operation_id)
            if operation is None or (
                str(operation.command.device_ref.owner_domain_id) != owner_scope
                or operation.command.device_ref.device_instance_id != device_id
            ):
                raise KeyError(operation_id)
            await authorize_exact_status(
                authorization=authorization,
                device_ref=operation.command.device_ref,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="forbidden") from exc
        return operation.status

    return router
