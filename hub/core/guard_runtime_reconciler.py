"""Deliver durable GuardBinding runtime desired state through eidolon.control."""

from __future__ import annotations

import logging
from typing import Any

from eidolon_data import DataStore
from eidolon_sdk.biz.contracts import CONTROL_OP_GUARD_RUNTIME_SYNC

from hub.core.admin_runtime import LiveKitAdminRuntime

logger = logging.getLogger(__name__)


class GuardRuntimeReconciler:
    """Turn persisted Guard runtime deliveries into idempotent device commands.

    The delivery table is authoritative. LiveKit is only the transport: an
    offline device leaves its delivery pending, a Hub crash releases its lease,
    and a device result is mapped back using the generic command id.
    """

    def __init__(self, store: DataStore, runtime: LiveKitAdminRuntime) -> None:
        self._store = store
        self._runtime = runtime

    async def reconcile_once(self, *, limit: int = 50) -> int:
        dispatched = 0
        for ready in await self._store.guard_runtime_deliveries.list_ready(limit=limit):
            delivery = await self._store.guard_runtime_deliveries.claim_for_dispatch(
                ready.delivery_id
            )
            if delivery is None:
                continue
            payload = {
                "binding_id": delivery.binding_id,
                "runtime_revision": delivery.runtime_revision,
                "desired_runtime_state": delivery.desired_runtime_state,
            }
            try:
                command = await self._runtime.send_command(
                    delivery.device_id,
                    payload,
                    op=CONTROL_OP_GUARD_RUNTIME_SYNC,
                    ttl_ms=60_000,
                    qos="result",
                    priority="urgent"
                    if delivery.desired_runtime_state == "stopped"
                    else "high",
                )
            except ValueError as exc:
                # Offline is routine. Keep the delivery durable and retry after
                # the normal probe loop sees the device return.
                await self._store.guard_runtime_deliveries.mark_retry(
                    delivery.delivery_id,
                    error=str(exc),
                )
                continue
            except Exception as exc:  # pragma: no cover - transport boundary
                logger.exception("Guard runtime delivery send failed delivery_id=%s", delivery.delivery_id)
                await self._store.guard_runtime_deliveries.mark_retry(
                    delivery.delivery_id,
                    error=str(exc),
                )
                continue
            if await self._store.guard_runtime_deliveries.mark_dispatched(
                delivery.delivery_id,
                command_id=str(command["command_id"]),
            ) is not None:
                dispatched += 1
        return dispatched

    async def apply_command_result(self, command: dict[str, Any]) -> None:
        if command.get("op") != CONTROL_OP_GUARD_RUNTIME_SYNC:
            return
        command_id = command.get("command_id")
        status = command.get("status")
        if not isinstance(command_id, str) or not isinstance(status, str):
            return
        if status not in {"succeeded", "failed", "rejected", "expired", "timeout"}:
            return
        result = command.get("result")
        result_json = result if isinstance(result, dict) else None
        error = str(command.get("error") or "")
        delivery = await self._store.guard_runtime_deliveries.record_command_result(
            command_id,
            status=status,
            result_json=result_json,
            error=error,
        )
        if delivery is None:
            return
        applied = delivery.status == "applied"
        await self._store.events.record_event(
            event_id=f"guard_runtime_delivery_{delivery.delivery_id}_{delivery.status}",
            owner_id=delivery.owner_id,
            subject_type="guard_binding",
            subject_id=delivery.binding_id,
            event_type="guard.runtime.applied" if applied else "guard.runtime.failed",
            actor_type="hub_runtime",
            actor_id=delivery.device_id,
            payload_json={
                "runtime_revision": delivery.runtime_revision,
                "desired_runtime_state": delivery.desired_runtime_state,
                "command_id": command_id,
                "result": result_json or {},
                "error": delivery.last_error,
            },
        )
