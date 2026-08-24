"""HTTPS DeviceDeliveryPort adapter for signed erase pull and ACK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from hub.contracts.bindings.device import (
    DeliverEnvelope,
    DeliveryAcceptance,
    DeviceEraseContractError,
    DeviceEvidenceEnvelope,
    DeviceLocalEraseAck,
    DeviceLocalEraseOperationStatus,
    DeviceRef,
)
from hub.ports.identity import ManagementAuthorizer, ManagementPermission

from .application import (
    AcknowledgeDeviceEraseOperation,
    PullDeviceEraseOperation,
    ReconcileDeviceEraseOperations,
)
from .domain import DeviceEraseGenerationConflict, DeviceEraseIdempotencyConflict
from .ports import DeviceEraseLedger


class _AdapterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PullEraseOperationRequest(_AdapterModel):
    device_ref: DeviceRef
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    public_key_spki: str = Field(min_length=120, max_length=256)
    device_signature: str = Field(min_length=86, max_length=86)


@dataclass(frozen=True, slots=True)
class DeviceEraseHttpServices:
    pull: PullDeviceEraseOperation
    acknowledge: AcknowledgeDeviceEraseOperation
    reconcile: ReconcileDeviceEraseOperations
    ledger: DeviceEraseLedger
    authorizer: ManagementAuthorizer


def create_device_erase_router(
    services: DeviceEraseHttpServices | Callable[[], DeviceEraseHttpServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/device-control/v1", tags=["device-control"])

    def current() -> DeviceEraseHttpServices:
        return services() if callable(services) else services

    @router.post(
        "/erase-operations:pull",
        response_model=DeliverEnvelope,
        responses={204: {"description": "No non-terminal operation for this generation"}},
    )
    async def pull_operation(
        payload: PullEraseOperationRequest, response: Response
    ) -> DeliverEnvelope | None:
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
            response.status_code = status.HTTP_204_NO_CONTENT
            return None
        return DeliverEnvelope(
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
            if (
                payload.operation_id != operation_id
                or payload.device_ref != evidence.device_ref
            ):
                raise DeviceEraseGenerationConflict(
                    "evidence and nested ACK DeviceRef differ"
                )
            current_operation = await current().ledger.get(operation_id=operation_id)
            if current_operation is None:
                raise KeyError(operation_id)
            if (
                current_operation.command.device_ref != evidence.device_ref
                or current_operation.delivery_attempt_id
                != evidence.delivery_attempt_id
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
            await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_CONTROL_GET,
                owner_scope=owner_scope,
                device_id=device_id,
            )
            operation = await runtime.ledger.get(operation_id=operation_id)
            if operation is None or (
                str(operation.command.device_ref.owner_domain_id) != owner_scope
                or operation.command.device_ref.device_instance_id != device_id
            ):
                raise KeyError(operation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="forbidden") from exc
        return operation.status

    return router
