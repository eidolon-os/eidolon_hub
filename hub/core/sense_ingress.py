"""Owner-scoped perception ingress (co-presence, D1 ADR 1A').

sense.* facts are the vision organ's output. Unlike the guard plane, perception
is **owner-scoped ambient truth** — attributed to the owner + the observing
device, NOT to a companion. Whichever companion is currently engaged consumes
it downstream (agent fusion). This ingress therefore validates against the
general device model (device belongs to the owner, not disabled), not a
GuardBinding, and records an owner-scoped audit event.

It deliberately does NOT: create policy actions / an outbox entry (that is the
guard/sentinel plane), nor project owner-presence state yet (that attaches when
the owner-presence runtime state lands; see ADR §8 open questions).
"""

from __future__ import annotations

from uuid import uuid4

from eidolon_data import DataStore
from eidolon_sdk.biz.sense import SenseEvent, SenseFatigue, parse_sense_message


class SenseIngressError(Exception):
    """Raised when a sense.* fact fails owner-scoped validation."""


class SenseIngress:
    """Validate + audit owner-scoped sense.* facts. No guard/GuardBinding coupling."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def ingest(self, payload: object, *, source: str = "vision_worker") -> dict:
        message = parse_sense_message(payload)

        # Source boundary: the host Vision Worker distills only fatigue/scene
        # (T2). attention/session are device-origin (T0) via a different path.
        if source == "vision_worker" and not isinstance(message, (SenseFatigue, SenseEvent)):
            raise SenseIngressError(
                "vision worker may only emit sense.fatigue / sense.event facts"
            )

        # Owner-scoped validation: the observing device must belong to the
        # asserted owner and be usable. Owner is the trust scope — no companion.
        device = await self._store.devices.get_device(message.device_id)
        if device is None or device.owner_id != message.owner_id:
            raise SenseIngressError("sense fact device/owner does not match a known device")
        if device.revoked_at is not None or device.status in {"disabled", "revoked"}:
            raise SenseIngressError("sense source device is disabled or revoked")

        await self._store.events.record_event(
            event_id=f"sense_{uuid4().hex}",
            owner_id=message.owner_id,
            subject_type="device",
            subject_id=message.device_id,
            event_type=message.type,
            actor_type=source,
            actor_id=message.device_id,
            payload_json=message.model_dump(mode="json"),
        )
        return message.model_dump(mode="json")
