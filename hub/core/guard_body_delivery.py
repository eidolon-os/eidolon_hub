"""Deliver Guard policy actions to body devices through standard commands."""

from __future__ import annotations

import logging
from time import time
from typing import Any

from eidolon_data import DataStore
from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET
from eidolon_sdk.biz.guard import GuardPolicyActionAck

from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.body_presence_dispatcher import BodyPresenceDispatcher
from hub.core.guard_policy import GuardControlPlane, GuardPolicyError

logger = logging.getLogger(__name__)

DEFAULT_MAX_DELIVERY_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 1
MAX_RETRY_DELAY_SECONDS = 60


class GuardBodyActionDeliveryWorker:
    """Map durable Guard body actions to the existing device command path."""

    def __init__(
        self,
        store: DataStore,
        runtime: LiveKitAdminRuntime,
        control_plane: GuardControlPlane,
        *,
        dispatcher: BodyPresenceDispatcher | None = None,
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
        retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
    ) -> None:
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must not be negative")
        self._store = store
        self._runtime = runtime
        self._control_plane = control_plane
        # The reflex path shares the same command seam as the manual admin
        # wiggle; keep runtime for get_command result reconciliation.
        self._dispatcher = dispatcher or BodyPresenceDispatcher(runtime)
        self._max_delivery_attempts = max_delivery_attempts
        self._retry_base_seconds = retry_base_seconds

    async def reconcile_once(self, *, limit: int = 50) -> int:
        await self.reconcile_dead_letters(limit=limit)
        dispatched = 0
        rows = await self._store.guard_actions.list_ready_body_deliveries(
            action=BODY_OP_PRESENCE_SET,
            limit=limit,
        )
        for row in rows:
            claimed = await self._store.guard_actions.claim_for_dispatch(row.action_id)
            if claimed is None or not claimed.delivery_claim_token:
                continue
            try:
                command = await self._dispatcher.dispatch(
                    claimed.subscriber,
                    state=str((claimed.payload_json or {}).get("state") or "awake"),
                    correlation_id=claimed.correlation_id,
                    action_id=claimed.action_id,
                    guard_epoch=claimed.guard_epoch,
                    ttl_ms=30_000,
                    qos="result",
                    priority="normal",
                )
            except ValueError as exc:
                await self._release_after_error(claimed, str(exc))
                continue
            except Exception as exc:  # pragma: no cover - transport boundary
                logger.exception("Guard body action dispatch failed action_id=%s", claimed.action_id)
                await self._release_after_error(claimed, str(exc))
                continue
            if await self._store.guard_actions.mark_dispatched(
                claimed.action_id,
                claim_token=claimed.delivery_claim_token,
                command_id=str(command["command_id"]),
            ) is not None:
                dispatched += 1
        await self.reconcile_dead_letters(limit=limit)
        return dispatched

    async def reconcile_dead_letters(self, *, limit: int = 50) -> int:
        """Close actions whose bounded retry budget was exhausted."""
        acknowledged = 0
        rows = await self._store.guard_actions.list_dead_letter_body_deliveries(
            action=BODY_OP_PRESENCE_SET,
            limit=limit,
        )
        for row in rows:
            ack = GuardPolicyActionAck(
                guard_companion_id=row.guard_companion_id,
                device_id=row.device_id,
                correlation_id=row.correlation_id,
                guard_epoch=row.guard_epoch,
                ts_ms=max(int(time() * 1000), int(row.published_at.timestamp() * 1000)),
                action_id=row.action_id,
                subscriber=row.subscriber,
                status="failed",
                message=(
                    f"body delivery exhausted after {row.delivery_attempt_count} attempts: "
                    f"{row.last_error}"
                )[:256],
            )
            try:
                await self._control_plane.handle(
                    ack.model_dump(mode="json"),
                    source="body_delivery",
                )
            except GuardPolicyError:
                logger.warning("Guard body dead letter rejected action_id=%s", row.action_id)
                continue
            acknowledged += 1
        return acknowledged

    async def reconcile_command_results(self, *, limit: int = 50) -> int:
        """Close durable action rows whose standard body command is terminal."""
        reconciled = 0
        rows = await self._store.guard_actions.list_dispatched_body_deliveries(
            action=BODY_OP_PRESENCE_SET,
            limit=limit,
        )
        for row in rows:
            if not row.command_id:
                continue
            command = await self._runtime.get_command(row.command_id)
            if command is None:
                continue
            if command.get("status") not in {"succeeded", "failed", "rejected", "expired", "timeout"}:
                continue
            await self.apply_command_result(command)
            reconciled += 1
        return reconciled

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

    async def _release_after_error(self, row, error: str) -> None:
        delay_seconds = min(
            self._retry_base_seconds * (2 ** max(row.delivery_attempt_count - 1, 0)),
            MAX_RETRY_DELAY_SECONDS,
        )
        await self._store.guard_actions.release_claim_after_error(
            row.action_id,
            claim_token=row.delivery_claim_token,
            error=error,
            max_attempts=self._max_delivery_attempts,
            retry_delay_seconds=delay_seconds,
        )


def _result_matches_action(result: Any, action_id: str) -> bool:
    if not isinstance(result, dict):
        return False
    return result.get("action_id") == action_id
