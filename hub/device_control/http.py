"""HTTP adapters for cleanup-key binding, signed delivery pull and ACK."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from hub.contracts.bindings.channel import ChannelAssignment
from hub.contracts.bindings.device import (
    DeviceEraseContractError,
    DeviceLifecycleState,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceLocalEraseOperationStatus,
    DeviceOperationKeyProof,
    DeviceRef,
)
from hub.ports.identity import ManagementAuthorizer, ManagementPermission

from .application import (
    AcknowledgeDeviceEraseOperation,
    BindDeviceOperationKey,
    PullDeviceConfiguration,
    PullDeviceEraseOperation,
    ReconcileDeviceEraseOperations,
)
from .domain import DeviceEraseGenerationConflict, DeviceEraseIdempotencyConflict
from .ports import DeviceEraseLedger


class _AdapterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BindOperationKeyRequest(_AdapterModel):
    enrollment_id: str = Field(min_length=3, max_length=128)
    retrieval_token: str = Field(min_length=32, max_length=256, repr=False)
    proof: DeviceOperationKeyProof


class BindOperationKeyResult(_AdapterModel):
    operation: str = "device-control.operation-key-bound"
    key_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PullEraseOperationRequest(_AdapterModel):
    device_ref: DeviceRef
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    public_key_spki: str = Field(min_length=120, max_length=256)
    device_signature: str = Field(min_length=86, max_length=86)


class EraseOperationDelivery(_AdapterModel):
    operation: str = "device-control.erase-operation-delivery"
    delivery_attempt_id: str = Field(min_length=3, max_length=128)
    command: DeviceLocalEraseCommand


class PullDeviceConfigurationRequest(_AdapterModel):
    device_ref: DeviceRef
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    public_key_spki: str = Field(min_length=120, max_length=256)
    device_signature: str = Field(min_length=86, max_length=86)


class DeviceConfigurationResult(_AdapterModel):
    operation: str = "device-control.configuration"
    nonce: str
    device_ref: DeviceRef
    lifecycle_state: DeviceLifecycleState
    channels: tuple[ChannelAssignment, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceEraseHttpServices:
    bind_key: BindDeviceOperationKey
    pull: PullDeviceEraseOperation
    configuration: PullDeviceConfiguration
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

    @router.post("/operation-key-bindings", response_model=BindOperationKeyResult)
    async def bind_operation_key(payload: BindOperationKeyRequest) -> BindOperationKeyResult:
        try:
            key_id = await current().bind_key.execute(
                enrollment_id=payload.enrollment_id,
                retrieval_token=payload.retrieval_token,
                proof=payload.proof,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="enrollment not found") from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=410, detail="binding window expired") from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="operation key proof rejected") from exc
        except DeviceEraseIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
        return BindOperationKeyResult(key_id=key_id)

    @router.post(
        "/configuration:pull",
        response_model=DeviceConfigurationResult,
    )
    async def pull_configuration(
        payload: PullDeviceConfigurationRequest,
    ) -> DeviceConfigurationResult:
        try:
            outcome = await current().configuration.execute(
                device_ref=payload.device_ref,
                public_key_spki=payload.public_key_spki,
                nonce=payload.nonce,
                signature=payload.device_signature,
            )
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="STALE_GENERATION") from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device proof rejected") from exc
        return DeviceConfigurationResult(
            nonce=payload.nonce,
            device_ref=outcome.device_ref,
            lifecycle_state=outcome.lifecycle_state,
            channels=tuple(
                ChannelAssignment(
                    channel_id=grant.channel_id,
                    purpose=grant.purpose,
                    kinds=tuple(sorted(kind.value for kind in grant.kinds)),
                    binding_format=grant.binding_format,
                    issued_at_ms=int(grant.issued_at.timestamp() * 1000),
                    expires_at_ms=int(grant.expires_at.timestamp() * 1000),
                    opaque_binding=base64.b64encode(
                        grant.opaque_binding.relay_bytes()
                    ).decode("ascii"),
                )
                for grant in (
                    outcome.assignments.grants
                    if outcome.assignments is not None
                    else ()
                )
            ),
        )

    @router.post(
        "/erase-operations:pull",
        response_model=EraseOperationDelivery,
        responses={204: {"description": "No non-terminal operation for this generation"}},
    )
    async def pull_operation(
        payload: PullEraseOperationRequest, response: Response
    ) -> EraseOperationDelivery | None:
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
        return EraseOperationDelivery(
            delivery_attempt_id=delivery.delivery_attempt_id,
            command=delivery.command,
        )

    @router.post(
        "/erase-operations/{operation_id}/ack",
        response_model=DeviceLocalEraseOperationStatus,
    )
    async def acknowledge_operation(
        operation_id: str, payload: DeviceLocalEraseAck
    ) -> DeviceLocalEraseOperationStatus:
        if payload.operation_id != operation_id:
            raise HTTPException(status_code=422, detail="path and ACK operation_id differ")
        try:
            operation = await current().acknowledge.execute(ack=payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation not found") from exc
        except DeviceEraseGenerationConflict as exc:
            raise HTTPException(status_code=409, detail="STALE_GENERATION") from exc
        except DeviceEraseIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
        except (PermissionError, DeviceEraseContractError) as exc:
            raise HTTPException(status_code=403, detail="device ACK signature rejected") from exc
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
