"""Deliver durable Owner Face Profile desired state through eidolon.control."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

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
    def __init__(
        self,
        store: DataStore,
        runtime: LiveKitAdminRuntime,
        *,
        retry_base_seconds: float = 2,
        retry_max_seconds: float = 30,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    def _retry_delay(self, attempt_count: int) -> float:
        return min(
            self._retry_base_seconds * (2 ** max(attempt_count - 1, 0)),
            self._retry_max_seconds,
        )

    async def reconcile_once(self, *, limit: int = 50) -> int:
        dispatched = 0
        for ready in await self._store.guard_owner_face_profile_deliveries.list_ready(
            limit=limit
        ):
            delivery = (
                await self._store.guard_owner_face_profile_deliveries.claim_for_dispatch(
                    ready.delivery_id,
                    command_id=str(uuid4()),
                )
            )
            if delivery is None:
                continue
            if delivery.command_id is None:  # pragma: no cover - repository invariant
                logger.error(
                    "Owner Face Profile claim has no command_id delivery_id=%s",
                    delivery.delivery_id,
                )
                continue
            payload = GuardOwnerFaceProfileSync(
                binding_id=delivery.binding_id,
                profile_id=delivery.profile_id,
                profile_revision=delivery.profile_revision,
                desired_state=delivery.desired_state,
            ).model_dump(mode="json")
            existing = await self._runtime.get_command(delivery.command_id)
            if existing is not None:
                existing_status = existing.get("status")
                if existing_status in {"sent", "accepted", "running"}:
                    if (
                        await self._store.guard_owner_face_profile_deliveries.mark_dispatched(
                            delivery.delivery_id,
                            command_id=delivery.command_id,
                        )
                        is not None
                    ):
                        dispatched += 1
                    continue
                if existing_status in {
                    "succeeded",
                    "rejected",
                    "expired",
                    "timeout",
                } or (
                    existing_status == "failed"
                    and (existing.get("ack") is not None or existing.get("result") is not None)
                ):
                    marked = await self._store.guard_owner_face_profile_deliveries.mark_dispatched(
                        delivery.delivery_id,
                        command_id=delivery.command_id,
                    )
                    if marked is not None:
                        await self.apply_command_result(existing)
                        dispatched += 1
                    continue
                if existing_status == "failed":
                    await self._store.guard_owner_face_profile_deliveries.mark_retry(
                        delivery.delivery_id,
                        error=str(existing.get("error") or "command transport failed"),
                        retry_after_seconds=self._retry_delay(delivery.attempt_count),
                    )
                    continue
            try:
                command = await self._runtime.send_command(
                    delivery.device_id,
                    payload,
                    command_id=delivery.command_id,
                    op=CONTROL_OP_GUARD_OWNER_FACE_PROFILE_SYNC,
                    ttl_ms=300_000,
                    qos="result",
                    priority="urgent" if delivery.desired_state == "cleared" else "high",
                )
            except ValueError as exc:
                await self._store.guard_owner_face_profile_deliveries.mark_retry(
                    delivery.delivery_id,
                    error=str(exc),
                    retry_after_seconds=self._retry_delay(delivery.attempt_count),
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
                    retry_after_seconds=self._retry_delay(delivery.attempt_count),
                )
                continue
            if (
                await self._store.guard_owner_face_profile_deliveries.mark_dispatched(
                    delivery.delivery_id,
                    command_id=delivery.command_id,
                )
                is not None
            ):
                dispatched += 1
        return dispatched

    async def reconcile_command_results(self, *, limit: int = 50) -> int:
        """Apply persisted terminal command facts to durable profile deliveries.

        Device results normally arrive through the live control bridge. Hub-generated
        timeouts do not traverse that bridge, so the periodic reconciler must close or
        retry their durable delivery rows explicitly.
        """
        reconciled = 0
        for command in await self._runtime.list_commands(limit=limit):
            if command.get("op") != CONTROL_OP_GUARD_OWNER_FACE_PROFILE_SYNC:
                continue
            if command.get("status") not in {
                "succeeded",
                "failed",
                "rejected",
                "expired",
                "timeout",
            }:
                continue
            try:
                if await self.apply_command_result(command):
                    reconciled += 1
            except Exception:  # noqa: BLE001 - isolate one corrupt historical fact
                logger.exception(
                    "Owner Face Profile result reconciliation failed command_id=%s",
                    command.get("command_id"),
                )
        return reconciled

    async def apply_command_result(self, command: dict[str, Any]) -> bool:
        if command.get("op") != CONTROL_OP_GUARD_OWNER_FACE_PROFILE_SYNC:
            return False
        command_id = command.get("command_id")
        command_status = command.get("status")
        if not isinstance(command_id, str) or not isinstance(command_status, str):
            return False
        if command_status not in {"succeeded", "failed", "rejected", "expired", "timeout"}:
            return False
        current_delivery = (
            await self._store.guard_owner_face_profile_deliveries.get_by_command_id(
                command_id
            )
        )
        if current_delivery is None or current_delivery.status != "dispatched":
            return False
        error = str(command.get("error") or "")
        delivery = None
        if command_status in {"expired", "timeout"} or error in _TRANSIENT_DEVICE_ERRORS:
            device_attempted = (
                command.get("ack") is not None or command.get("result") is not None
            )
            delivery = (
                await self._store.guard_owner_face_profile_deliveries.retry_command_result(
                    command_id,
                    error=error or f"owner face command {command_status}",
                    max_attempts=5,
                    retry_after_seconds=self._retry_delay(
                        current_delivery.attempt_count
                    ),
                    device_attempted=device_attempted,
                )
            )
            if delivery is None or delivery.status == "pending":
                return delivery is not None
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
            return False
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
        return True
