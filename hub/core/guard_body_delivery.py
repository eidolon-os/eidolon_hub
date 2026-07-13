"""Deliver Guard policy actions to body devices through standard commands."""

from __future__ import annotations

import logging
from time import time
from typing import Any

from eidolon_data import DataStore
from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET
from eidolon_sdk.biz.guard import GuardPolicyActionAck

from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.guard_policy import GuardControlPlane, GuardPolicyError

logger = logging.getLogger(__name__)


class GuardBodyActionDeliveryWorker:
    """Map durable Guard body actions to the existing device command path."""

    def __init__(
        self,
        store: DataStore,
        runtime: LiveKitAdminRuntime,
        control_plane: GuardControlPlane,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._control_plane = control_plane

    async def reconcile_once(self, *, limit: int = 50) -> int:
        dispatched = 0
        rows = await self._store.guard_actions.list_ready_body_deliveries(
            action=BODY_OP_PRESENCE_SET,
            limit=limit,
        )
        for row in rows:
            payload = {
                "state": str((row.payload_json or {}).get("state") or "awake"),
                "guard_epoch": row.guard_epoch,
                "correlation_id": row.correlation_id,
                "action_id": row.action_id,
            }
            try:
                command = await self._runtime.send_command(
                    row.subscriber,
                    payload,
                    op=BODY_OP_PRESENCE_SET,
                    ttl_ms=30_000,
                    qos="result",
                    priority="normal",
                )
            except ValueError as exc:
                await self._store.guard_actions.record_delivery_error(
                    row.action_id,
                    error=str(exc),
                )
                continue
            except Exception as exc:  # pragma: no cover - transport boundary
                logger.exception("Guard body action dispatch failed action_id=%s", row.action_id)
                await self._store.guard_actions.record_delivery_error(
                    row.action_id,
                    error=str(exc),
                )
                continue
            if await self._store.guard_actions.mark_dispatched(
                row.action_id,
                command_id=str(command["command_id"]),
            ) is not None:
                dispatched += 1
        return dispatched

    async def apply_command_result(self, command: dict[str, Any]) -> None:
        if command.get("op") != BODY_OP_PRESENCE_SET:
            return
        command_id = command.get("command_id")
        status = command.get("status")
        if not isinstance(command_id, str) or not isinstance(status, str):
            return
        if status not in {"succeeded", "failed", "rejected", "expired", "timeout"}:
            return
        row = await self._store.guard_actions.get_by_command_id(command_id)
        if row is None or row.action != BODY_OP_PRESENCE_SET:
            return
        ack_status = "completed" if status == "succeeded" else "failed"
        message = str(command.get("error") or command.get("message") or "")
        result = command.get("result")
        if ack_status == "completed" and not _result_matches_action(result, row.action_id):
            ack_status = "failed"
            message = "body presence result does not match guard action"
        ack = GuardPolicyActionAck(
            guard_companion_id=row.guard_companion_id,
            device_id=row.device_id,
            correlation_id=row.correlation_id,
            guard_epoch=row.guard_epoch,
            ts_ms=max(int(time() * 1000), int(row.published_at.timestamp() * 1000)),
            action_id=row.action_id,
            subscriber=row.subscriber,
            status=ack_status,
            message=message[:256],
        )
        try:
            await self._control_plane.handle(
                ack.model_dump(mode="json"),
                source="body_delivery",
            )
        except GuardPolicyError:
            logger.warning("Guard body result rejected action_id=%s command_id=%s", row.action_id, command_id)


def _result_matches_action(result: Any, action_id: str) -> bool:
    if not isinstance(result, dict):
        return False
    return result.get("action_id") == action_id
