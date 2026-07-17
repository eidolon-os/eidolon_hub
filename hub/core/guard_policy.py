"""Guard application service: ingress validation, policy and durable outbox."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from eidolon_data import DataStore
from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET
from eidolon_sdk.biz.contracts import CONTROL_TOPIC
from eidolon_sdk.biz.guard import (
    GuardPolicyAction,
    GuardPolicyActionAck,
    GuardPresenceAbsent,
    GuardPresenceCandidate,
    GuardPresenceVerified,
    parse_guard_message,
    parse_guard_policy_config,
)

from hub.core.guard_policy_engine import GuardPolicyDecision, GuardPolicyEngine


class GuardPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class GuardPolicyResult:
    accepted: dict
    action: dict | None = None
    actions: list[dict] | None = None
    topic: str = CONTROL_TOPIC


class GuardControlPlane:
    """Coordinate the strict event contract with the persisted control plane.

    This layer deliberately does not know LiveKit rooms, Channel, StackChan, or
    device execution. A transport adapter supplies facts and a subscriber later
    consumes the durable action outbox.
    """

    def __init__(
        self,
        store: DataStore,
        *,
        policy_engine: GuardPolicyEngine | None = None,
        runtime_blackboard=None,
    ) -> None:
        self._store = store
        self._policy = policy_engine or GuardPolicyEngine()
        self._runtime_blackboard = runtime_blackboard

    async def handle(
        self,
        payload: object,
        *,
        sender_identity: str = "",
        source: str = "fixture",
    ) -> GuardPolicyResult:
        message = parse_guard_message(payload)
        self._assert_source_boundary(message, source)
        if (
            sender_identity
            and not isinstance(message, GuardPolicyActionAck)
            and sender_identity != message.device_id
        ):
            raise GuardPolicyError("guard sender identity does not match device_id")
        binding = await self._binding_for(message.device_id, message.guard_companion_id)

        if isinstance(message, GuardPolicyActionAck):
            return await self._accept_ack(binding.owner_id, message)
        if not isinstance(
            message,
            (GuardPresenceCandidate, GuardPresenceVerified, GuardPresenceAbsent),
        ):
            raise GuardPolicyError("Hub accepts Guard facts and subscriber acknowledgements only")

        await self._record_fact(binding.owner_id, message)
        replayed = await self._store.guard_actions.list_for_fact(
            binding_id=binding.binding_id,
            correlation_id=message.correlation_id,
            guard_epoch=message.guard_epoch,
            fact_type=message.type,
        )
        if replayed:
            actions = [self._action_from_row(row) for row in replayed]
            await self._record_evaluation(
                binding.owner_id,
                message,
                binding.policy_id,
                binding.config_revision,
                "replayed",
            )
            return GuardPolicyResult(
                accepted=message.model_dump(mode="json"),
                action=actions[0],
                actions=actions,
            )
        try:
            config = parse_guard_policy_config(binding.policy_id, binding.config_json)
        except ValueError as exc:
            raise GuardPolicyError("active guard binding policy configuration is invalid") from exc
        decision = self._policy.decide(
            message,
            policy_id=binding.policy_id,
            config=config,
        )
        decisions: list[GuardPolicyDecision] = []
        if decision is not None:
            decisions.append(decision)
            decisions.extend(await self._body_presence_decisions(binding.owner_id, message))
        await self._record_evaluation(
            binding.owner_id,
            message,
            binding.policy_id,
            binding.config_revision,
            decisions,
        )
        if not decisions:
            return GuardPolicyResult(accepted=message.model_dump(mode="json"))

        action_messages: list[GuardPolicyAction] = []
        for item in decisions:
            action = GuardPolicyAction(
                guard_companion_id=message.guard_companion_id,
                device_id=message.device_id,
                correlation_id=message.correlation_id,
                guard_epoch=message.guard_epoch,
                ts_ms=message.ts_ms,
                action_id=f"guard_action_{uuid4().hex}",
                policy_id=binding.policy_id,
                action=item.action,
                subscriber=item.subscriber,
                payload=item.payload,
            )
            action_messages.append(action)

        published = await self._store.guard_actions.publish_many(
            actions=[
                {
                    "action_id": action.action_id,
                    "binding_id": binding.binding_id,
                    "owner_id": binding.owner_id,
                    "guard_companion_id": action.guard_companion_id,
                    "device_id": action.device_id,
                    "correlation_id": action.correlation_id,
                    "guard_epoch": action.guard_epoch,
                    "fact_type": message.type,
                    "policy_id": action.policy_id,
                    "action": action.action,
                    "subscriber": action.subscriber,
                    "payload_json": action.payload,
                }
                for action in action_messages
            ]
        )
        if published is None:
            replayed = await self._store.guard_actions.list_for_fact(
                binding_id=binding.binding_id,
                correlation_id=message.correlation_id,
                guard_epoch=message.guard_epoch,
                fact_type=message.type,
            )
            if not replayed:
                raise GuardPolicyError("guard fact replay is still being committed")
            actions = [self._action_from_row(row) for row in replayed]
            return GuardPolicyResult(
                accepted=message.model_dump(mode="json"),
                action=actions[0],
                actions=actions,
            )

        actions: list[dict] = []
        for action in action_messages:
            await self._record_action(binding.owner_id, action)
            actions.append(action.model_dump(mode="json"))
        return GuardPolicyResult(
            accepted=message.model_dump(mode="json"),
            action=actions[0],
            actions=actions,
        )

    async def pending_actions(self, *, subscriber: str | None = None) -> list[dict]:
        rows = await self._store.guard_actions.list_pending(subscriber=subscriber)
        return [self._action_from_row(row) for row in rows]

    async def _binding_for(self, device_id: str, guard_companion_id: str):
        binding = await self._store.guard_bindings.get_active_for_device(device_id)
        if binding is None:
            raise GuardPolicyError("device has no active guard binding")
        if binding.guard_companion_id != guard_companion_id:
            raise GuardPolicyError("guard companion does not match the active device binding")
        return binding

    def _assert_source_boundary(self, message, source: str) -> None:
        if source in {"livekit", "fake_atk"}:
            if isinstance(message, (GuardPresenceCandidate, GuardPresenceAbsent)):
                return
            if isinstance(message, GuardPresenceVerified):
                raise GuardPolicyError("verified guard facts are fixture-only")
            if isinstance(message, GuardPolicyActionAck):
                raise GuardPolicyError("device ingress does not accept guard action acknowledgements")
            raise GuardPolicyError("device ingress only accepts candidate or absent guard facts")
        if source == "body_delivery" and not isinstance(message, GuardPolicyActionAck):
            raise GuardPolicyError("body delivery source may only acknowledge actions")

    async def _accept_ack(self, owner_id: str, ack: GuardPolicyActionAck) -> GuardPolicyResult:
        row = await self._store.guard_actions.get(ack.action_id)
        if row is None:
            raise GuardPolicyError("unknown guard policy action")
        if (
            row.owner_id != owner_id
            or row.guard_companion_id != ack.guard_companion_id
            or row.device_id != ack.device_id
            or row.correlation_id != ack.correlation_id
            or row.guard_epoch != ack.guard_epoch
            or row.subscriber != ack.subscriber
        ):
            raise GuardPolicyError("guard policy action ack does not match its action")
        requested_status = "acknowledged" if ack.status in {"accepted", "completed"} else "failed"
        if row.status != "published":
            if row.status != requested_status:
                raise GuardPolicyError("guard policy action terminal status cannot be overwritten")
            return GuardPolicyResult(accepted=ack.model_dump(mode="json"))
        await self._store.guard_actions.acknowledge(
            ack.action_id,
            status=ack.status,
            ack_json=ack.model_dump(mode="json"),
        )
        await self._store.events.record_event(
            event_id=f"guard_ack_{uuid4().hex}",
            owner_id=owner_id,
            subject_type="guard_binding",
            subject_id=ack.guard_companion_id,
            event_type="guard.policy.action_ack",
            actor_type="subscriber",
            actor_id=ack.subscriber,
            payload_json=ack.model_dump(mode="json"),
        )
        return GuardPolicyResult(accepted=ack.model_dump(mode="json"))

    async def _body_presence_decisions(
        self,
        owner_id: str,
        message: GuardPresenceCandidate | GuardPresenceVerified | GuardPresenceAbsent,
    ) -> list[GuardPolicyDecision]:
        if isinstance(message, GuardPresenceVerified):
            return []
        state = "awake" if isinstance(message, GuardPresenceCandidate) else "warm"
        presence = "candidate" if isinstance(message, GuardPresenceCandidate) else "absent"
        decisions: list[GuardPolicyDecision] = []
        seen: set[str] = set()
        if self._runtime_blackboard is None:
            return []
        for device in await self._runtime_blackboard.list_online_devices_for_owner(
            owner_id=owner_id
        ):
            if device.device_id == message.device_id:
                continue
            if device.capability(BODY_OP_PRESENCE_SET) is None:
                continue
            if device.device_id in seen:
                continue
            seen.add(device.device_id)
            decisions.append(
                GuardPolicyDecision(
                    action=BODY_OP_PRESENCE_SET,
                    subscriber=device.device_id,
                    payload={"state": state, "presence": presence},
                )
            )
        return decisions

    def _action_from_row(self, row) -> dict:
        return GuardPolicyAction(
            action_id=row.action_id,
            policy_id=row.policy_id,
            action=row.action,
            subscriber=row.subscriber,
            guard_companion_id=row.guard_companion_id,
            device_id=row.device_id,
            correlation_id=row.correlation_id,
            guard_epoch=row.guard_epoch,
            ts_ms=int((row.published_at or row.created_at).timestamp() * 1000),
            payload=row.payload_json or {},
        ).model_dump(mode="json")

    async def _record_fact(
        self,
        owner_id: str,
        message: GuardPresenceCandidate | GuardPresenceVerified | GuardPresenceAbsent,
    ) -> None:
        await self._store.events.record_event(
            event_id=f"guard_fact_{uuid4().hex}",
            owner_id=owner_id,
            subject_type="guard_binding",
            subject_id=message.guard_companion_id,
            event_type=message.type,
            actor_type="device",
            actor_id=message.device_id,
            payload_json=message.model_dump(mode="json"),
        )

    async def _record_action(self, owner_id: str, action: GuardPolicyAction) -> None:
        await self._store.events.record_event(
            event_id=f"guard_action_{uuid4().hex}",
            owner_id=owner_id,
            subject_type="guard_binding",
            subject_id=action.guard_companion_id,
            event_type=action.type,
            actor_type="hub_policy",
            actor_id=action.policy_id,
            payload_json=action.model_dump(mode="json"),
        )

    async def _record_evaluation(
        self,
        owner_id: str,
        message: GuardPresenceCandidate | GuardPresenceVerified | GuardPresenceAbsent,
        policy_id: str,
        config_revision: int,
        decision,
    ) -> None:
        if isinstance(decision, list):
            decision_value = [item.action for item in decision] or "no_action"
        elif isinstance(decision, str):
            decision_value = decision
        else:
            decision_value = decision.action if decision else "no_action"
        await self._store.events.record_event(
            event_id=f"guard_policy_{uuid4().hex}",
            owner_id=owner_id,
            subject_type="guard_binding",
            subject_id=message.guard_companion_id,
            event_type="guard.policy.evaluated",
            actor_type="hub_policy",
            actor_id=policy_id,
            payload_json={
                "correlation_id": message.correlation_id,
                "guard_epoch": message.guard_epoch,
                "config_revision": config_revision,
                "decision": decision_value,
            },
        )
