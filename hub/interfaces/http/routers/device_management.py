"""HTTP interface for the Hub device-management control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException

from hub.application.queries.get_device import GetDevice
from hub.application.queries.list_devices import DeviceListQuery, ListDevices
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.rename_device import RenameDevice
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.contracts.bindings.device import (
    ClaimEventPage,
    ClaimRevocationResult,
    DeviceApprovalRequest,
    DeviceControlOperationStatus,
    DeviceDirectoryEntry,
    DeviceDirectoryPage,
    DeviceLifecycleStatus,
    DeviceManagementEventPage,
    DeviceRenameRequest,
    DeviceRevocationRequest,
)
from hub.contracts.mappers import (
    claim_result_to_wire,
    device_control_operation_to_wire,
    directory_entry_to_wire,
    lifecycle_status_to_wire,
    stored_claim_event_to_wire,
    stored_event_to_wire,
)
from hub.domain.devices.entities import DeviceLifecycleState, DeviceRef
from hub.ports.claim_lifecycle import ClaimLifecycleStore
from hub.ports.device_control import DeviceControlStore
from hub.ports.identity import ManagementAuthorizer, ManagementPermission
from hub.ports.management_events import DeviceManagementEventStream


@dataclass(frozen=True, slots=True)
class DeviceManagementHttpServices:
    get_device: GetDevice
    list_devices: ListDevices
    approve_device: ApproveDevice
    rename_device: RenameDevice
    revoke_device: RevokeDevice
    authorizer: ManagementAuthorizer
    event_stream: DeviceManagementEventStream
    claim_events: ClaimLifecycleStore
    device_control: DeviceControlStore


def create_device_management_router(
    *, services: DeviceManagementHttpServices | Callable[[], DeviceManagementHttpServices]
) -> APIRouter:
    router = APIRouter(prefix="/api/device-management/v1", tags=["device-management"])

    def current() -> DeviceManagementHttpServices:
        return services() if callable(services) else services

    @router.get("/owners/{owner_scope}/devices", response_model=DeviceDirectoryPage)
    async def list_directory(
        owner_scope: str,
        lifecycle_state: DeviceLifecycleState | None = None,
        device_kind: str | None = None,
        capability: str | None = None,
        q: str | None = None,
        after: str | None = None,
        limit: int = 50,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceDirectoryPage:
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_LIST,
                owner_scope=owner_scope,
                device_id=None,
            )
            page = await runtime.list_devices.execute(
                DeviceListQuery(
                    owner_scope=owner_scope,
                    lifecycle_state=lifecycle_state,
                    device_kind=device_kind,
                    capability=capability,
                    q=q,
                    after=after,
                    limit=limit,
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return DeviceDirectoryPage(
            next_cursor=page.next_cursor,
            devices=tuple(directory_entry_to_wire(entry) for entry in page.entries),
        )

    @router.get(
        "/owners/{owner_scope}/devices/{device_id}",
        response_model=DeviceDirectoryEntry,
    )
    async def get_directory_entry(
        owner_scope: str,
        device_id: str,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceDirectoryEntry:
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_GET,
                owner_scope=owner_scope,
                device_id=device_id,
            )
            entry = await runtime.get_device.execute(
                owner_scope=owner_scope,
                device_id=device_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return directory_entry_to_wire(entry)

    @router.get("/owners/{owner_scope}/events", response_model=DeviceManagementEventPage)
    async def list_device_events(
        owner_scope: str,
        after_stream_position: int = 0,
        limit: int = 100,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceManagementEventPage:
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_EVENTS,
                owner_scope=owner_scope,
                device_id=None,
            )
            stored = await runtime.event_stream.list_after(
                owner_scope=owner_scope,
                stream_position=after_stream_position,
                limit=limit,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return DeviceManagementEventPage(
            next_stream_position=(stored[-1].stream_position if stored else after_stream_position),
            events=tuple(stored_event_to_wire(item) for item in stored),
        )

    @router.get("/claim-events", response_model=ClaimEventPage)
    async def list_claim_events(
        after_stream_position: int = 0,
        limit: int = 100,
        authorization: str = Header(alias="Authorization"),
    ) -> ClaimEventPage:
        runtime = current()
        try:
            await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.CLAIM_EVENTS,
                owner_scope=None,
                device_id=None,
            )
            stored = await runtime.claim_events.list_events_after(
                stream_position=after_stream_position,
                limit=limit,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ClaimEventPage(
            next_stream_position=(
                stored[-1].stream_position if stored else after_stream_position
            ),
            events=tuple(stored_claim_event_to_wire(item) for item in stored),
        )

    @router.get(
        "/owners/{owner_scope}/devices/{device_id}/control-operations/{event_id}",
        response_model=DeviceControlOperationStatus,
    )
    async def get_device_control_operation(
        owner_scope: str,
        device_id: str,
        event_id: str,
        authorization: str = Header(alias="Authorization"),
    ) -> DeviceControlOperationStatus:
        runtime = current()
        try:
            principal = await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_CONTROL_GET,
                owner_scope=owner_scope,
                device_id=device_id,
            )
            operation = await runtime.device_control.get_by_event_id(event_id=event_id)
            if operation is None:
                raise KeyError(event_id)
            if (
                operation.device_ref.owner_domain_id != owner_scope
                or operation.device_ref.device_instance_id != device_id
            ):
                raise KeyError(event_id)
            if "hub-admin" not in principal.roles and (
                principal.intent_id is None
                or principal.target_owner_domain_generation
                != operation.device_ref.owner_domain_generation
                or principal.target_claim_generation
                != operation.device_ref.claim_generation
                or principal.target_trust_epoch != operation.device_ref.trust_epoch
                or principal.target_manifest_digest
                != operation.device_ref.accepted_manifest_digest
            ):
                raise PermissionError(
                    "management credential is not bound to this Device Control operation"
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return device_control_operation_to_wire(operation)

    @router.post("/devices/{device_id}/approval", response_model=DeviceLifecycleStatus)
    async def approve_registered_device(
        device_id: str,
        payload: DeviceApprovalRequest,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            principal = await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_APPROVE,
                owner_scope=None,
                device_id=device_id,
            )
            device = await runtime.approve_device.execute(
                device_id=device_id,
                owner_id=payload.owner_id,
                request_id=payload.request_id,
                principal_id=principal.subject_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return lifecycle_status_to_wire(device)

    @router.patch("/devices/{device_id}", response_model=DeviceLifecycleStatus)
    async def rename_registered_device(
        device_id: str,
        payload: DeviceRenameRequest,
        authorization: str = Header(alias="Authorization"),
    ):
        """Set what a device is called.

        Gated on the same permission as revocation rather than a new one: both
        are an Owner acting on a device they hold, and inventing a permission
        for the smaller of the two would say they are different kinds of act.
        """

        runtime = current()
        try:
            principal = await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_REVOKE,
                owner_scope=None,
                device_id=device_id,
            )
            device = await runtime.rename_device.execute(
                device_id=device_id,
                owner_scope=payload.owner_scope,
                display_name=payload.display_name,
                principal_id=principal.subject_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return lifecycle_status_to_wire(device)

    @router.post("/devices/{device_id}/revocation", response_model=ClaimRevocationResult)
    async def revoke_registered_device(
        device_id: str,
        payload: DeviceRevocationRequest,
        authorization: str = Header(alias="Authorization"),
    ):
        runtime = current()
        try:
            principal = await runtime.authorizer.authorize(
                credential=authorization,
                permission=ManagementPermission.DEVICE_REVOKE,
                owner_scope=payload.device_ref.owner_domain_id,
                device_id=device_id,
            )
            if payload.device_ref.device_instance_id != device_id:
                raise ValueError("path device_id and DeviceRef do not match")
            if "hub-admin" not in principal.roles:
                if (
                    principal.intent_id != payload.correlation_id
                    or principal.target_device_id != device_id
                    or principal.target_owner_domain_generation
                    != payload.device_ref.owner_domain_generation
                    or principal.target_claim_generation
                    != payload.device_ref.claim_generation
                    or principal.target_trust_epoch != payload.device_ref.trust_epoch
                    or principal.target_manifest_digest
                    != payload.device_ref.accepted_manifest_digest
                ):
                    raise PermissionError(
                        "management credential is not bound to this RemovalIntent"
                    )
            result = await runtime.revoke_device.execute(
                device_ref=DeviceRef(**payload.device_ref.model_dump()),
                reason=payload.reason,
                command_id=payload.command_id,
                correlation_id=payload.correlation_id,
                principal_id=principal.actor_ref or principal.subject_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return claim_result_to_wire(result)

    return router
