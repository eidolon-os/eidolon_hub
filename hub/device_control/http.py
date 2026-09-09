"""HTTPS DeviceDeliveryPort adapter for signed erase pull and ACK."""

from __future__ import annotations

import logging
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

_LOG = logging.getLogger(__name__)


def _refused(
    code: str,
    *,
    surface: str,
    because: str,
    who_can_end_it: str,
    device_ref: DeviceRef,
) -> HTTPException:
    """Write down why this surface refused, then hand back the refusal to raise.

    Every conflict below answers 409, and 409 is the whole of what the access
    log keeps. So the host's own record of a device that pulls its
    configuration every thirty seconds and is refused every time was one line
    repeated forever, identical whichever refusal it was. The reason existed —
    it is the ``detail`` — but it went only to the device, which in the
    ``CLAIM_NOT_ACTIVE`` case is precisely the party that cannot act on it.

    What the operator needs first is not the code but who has to change
    something before this converges, and the codes differ in exactly that: a
    stale DeviceRef is the device's to fix by re-enrolling, a suspended Claim
    is the Owner's and no one else's. That sentence is written here, at the
    decision, not recovered later from a status line that never carried it.

    ``code`` is the same value the response carries, passed once, so the
    record and the wire cannot drift apart. No proof material is named: the
    nonce, the operational key and the signature are exactly the inputs the
    422 handler already refuses to reflect into a log collector.

    ERROR rather than WARNING, on the distinction this Hub already draws
    between the two: a warning is a convergence still pending, and none of
    these converge. Nothing the Authority does next ends any of them.
    """

    _LOG.error(
        "%s refused, and no retry ends it: %s. Who can: %s. "
        "device=%s owner-domain=%s generation=%s/%s/%s code=%s",
        surface,
        because,
        who_can_end_it,
        device_ref.device_instance_id,
        device_ref.owner_domain_id,
        device_ref.owner_domain_generation,
        device_ref.claim_generation,
        device_ref.trust_epoch,
        code,
    )
    return HTTPException(status_code=409, detail=code)


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
            raise _refused(
                "STALE_GENERATION",
                surface="Configuration pull",
                because=(
                    "no Claim stands at the exact DeviceRef presented — owner domain, "
                    "device, or one of the three generations is not what Admission recorded"
                ),
                who_can_end_it=(
                    "the device, by re-enrolling and presenting the DeviceRef it is "
                    "actually granted"
                ),
                device_ref=payload.device_ref,
            ) from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device proof rejected") from exc
        claim = configuration.claim
        if claim.state not in {"active", "revoked"}:
            raise _refused(
                "CLAIM_NOT_ACTIVE",
                surface="Configuration pull",
                # Naming the state is the point. "active" and "revoked" are both
                # answered; anything else is a Claim held open mid-decision, and
                # which one it is says what the Owner has left to do.
                because=f"the Claim at this DeviceRef is {claim.state!r}, neither active nor revoked",
                who_can_end_it=(
                    "the Owner, by resuming or revoking this Claim — the device cannot, "
                    "and re-enrolling will not help it"
                ),
                device_ref=claim.device_ref,
            )
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
            raise _refused(
                "CLAIM_NOT_ACTIVE",
                surface="Manifest assertion",
                # Three conditions raise this one KeyError: no Claim at this
                # exact DeviceRef, a Claim that is not active, and no device row
                # of this surface's own. The wire code names only the second, and
                # this line will not pretend to more than it was handed. Saying
                # where to look is worth more than guessing which one it was.
                because=(
                    "this DeviceRef has no active Claim, or no device row of its own; "
                    "the application layer collapses those into one refusal"
                ),
                who_can_end_it=(
                    "the Owner if the Claim is suspended, the device if it presents a "
                    "DeviceRef no Claim records — read admission_claims_v1 at this "
                    "DeviceRef to tell which"
                ),
                device_ref=payload.device_ref,
            ) from exc
        except ManifestRevisionConflict as exc:
            raise _refused(
                "MANIFEST_REVISION_CONFLICT",
                surface="Manifest assertion",
                # The domain error already says which of its two disagreements
                # this is, and neither spelling names anything the device signed.
                because=str(exc),
                who_can_end_it=(
                    "the device, by asserting above the revision the Authority already "
                    "accepted rather than at or below it"
                ),
                device_ref=payload.device_ref,
            ) from exc
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
            raise _refused(
                "STALE_GENERATION",
                surface="Erase ACK",
                because=str(exc),
                who_can_end_it=(
                    "the device, by acknowledging the Delivery attempt it was actually "
                    "handed, for the DeviceRef that attempt was addressed to"
                ),
                device_ref=evidence.device_ref,
            ) from exc
        except DeviceEraseIdempotencyConflict as exc:
            raise _refused(
                "IDEMPOTENCY_CONFLICT",
                surface="Erase ACK",
                because=str(exc),
                who_can_end_it=(
                    "the device, by not replaying a spent ack_sequence with different "
                    "content — the Authority keeps the first answer on purpose"
                ),
                device_ref=evidence.device_ref,
            ) from exc
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
