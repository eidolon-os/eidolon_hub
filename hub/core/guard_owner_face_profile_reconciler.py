"""Deliver durable Owner Face Profile desired state through eidolon.control."""

from __future__ import annotations

import logging
from typing import Any

from eidolon_data import DataStore
from eidolon_sdk.biz.contracts import CONTROL_OP_GUARD_OWNER_FACE_PROFILE_SYNC
from eidolon_sdk.biz.guard import GuardOwnerFaceProfileSync

from hub.core.admin_runtime import LiveKitAdminRuntime

logger = logging.getLogger(__name__)

_TRANSIENT_DEVICE_ERRORS = frozenset(
    {
        "OWNER_FACE_UNAVAILABLE",
        "OWNER_FACE_BUSY",
        "OWNER_FACE_RESULT_ENCODING_FAILED",
        "OWNER_FACE_MANIFEST_FETCH_FAILED",
        "OWNER_FACE_REFERENCE_FETCH_FAILED",
        "OWNER_FACE_COMMIT_FAILED",
    }
)


class GuardOwnerFaceProfileReconciler:
    def __init__(self, store: DataStore, runtime: LiveKitAdminRuntime) -> None:
        self._store = store
        self._runtime = runtime

    async def reconcile_once(self, *, limit: int = 50) -> int:
        dispatched = 0
        for ready in await self._store.guard_owner_face_profile_deliveries.list_ready(
            limit=limit
        ):
            delivery = (
                await self._store.guard_owner_face_profile_deliveries.claim_for_dispatch(
                    ready.delivery_id
                )
            )
            if delivery is None:
                continue
            payload = GuardOwnerFaceProfileSync(
                binding_id=delivery.binding_id,
                profile_id=delivery.profile_id,
                profile_revision=delivery.profile_revision,
                desired_state=delivery.desired_state,
            ).model_dump(mode="json")
            try:
                command = await self._runtime.send_command(
                    delivery.device_id,
                    payload,
                    op=CONTROL_OP_GUARD_OWNER_FACE_PROFILE_SYNC,
                    ttl_ms=300_000,
                    qos="result",
                    priority="urgent" if delivery.desired_state == "cleared" else "high",
                )
            except ValueError as exc:
                await self._store.guard_owner_face_profile_deliveries.mark_retry(
                    delivery.delivery_id,
                    error=str(exc),
                )
                continue
            except Exception as exc:  # pragma: no cover - transport boundary
                logger.exception(
                    "Owner Face Profile delivery send failed delivery_id=%s",
                    delivery.delivery_id,
                )
                await self._store.guard_owner_face_profile_deliveries.mark_retry(
                    delivery.delivery_id,
                    error=str(exc),
                )
                continue
            if (
                await self._store.guard_owner_face_profile_deliveries.mark_dispatched(
                    delivery.delivery_id,
                    command_id=str(command["command_id"]),
                )
                is not None
            ):
                dispatched += 1
        return dispatched

    async def apply_command_result(self, command: dict[str, Any]) -> None:
        if command.get("op") != CONTROL_OP_GUARD_OWNER_FACE_PROFILE_SYNC:
            return
        command_id = command.get("command_id")
        command_status = command.get("status")
        if not isinstance(command_id, str) or not isinstance(command_status, str):
            return
        if command_status not in {"succeeded", "failed", "rejected", "expired", "timeout"}:
            return
        error = str(command.get("error") or "")
        delivery = None
        if command_status in {"expired", "timeout"} or error in _TRANSIENT_DEVICE_ERRORS:
            delivery = (
                await self._store.guard_owner_face_profile_deliveries.retry_command_result(
                    command_id,
                    error=error or f"owner face command {command_status}",
                    max_attempts=5,
                )
            )
            if delivery is None or delivery.status == "pending":
                return
            # The bounded retry budget is exhausted; record the terminal fact below.
            command_status = "failed"
        result = command.get("result")
        result_json = result if isinstance(result, dict) else None
        if delivery is None:
            delivery = (
                await self._store.guard_owner_face_profile_deliveries.record_command_result(
                    command_id,
                    status=command_status,
                    result_json=result_json,
                    error=error,
                )
            )
        if delivery is None:
            return
        applied = delivery.status == "applied"
        await self._store.events.record_event(
            event_id=f"guard_owner_face_{delivery.delivery_id}_{delivery.status}",
            owner_id=delivery.owner_id,
            subject_type="owner_face_profile",
            subject_id=delivery.profile_id,
            event_type=(
                "guard.owner_face_profile.applied"
                if applied
                else "guard.owner_face_profile.failed"
            ),
            actor_type="hub_runtime",
            actor_id=delivery.device_id,
            data_classification="sensitive",
            payload_json={
                "profile_revision": delivery.profile_revision,
                "desired_state": delivery.desired_state,
                "command_id": command_id,
                "result": result_json or {},
                "error": delivery.last_error,
            },
        )
