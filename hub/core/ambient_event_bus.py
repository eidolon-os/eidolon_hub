"""Authenticated, owner-scoped fan-out for short-lived device events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from time import monotonic, time
from typing import Any, Callable

from eidolon_sdk.biz.contracts import EVENT_TOPIC
from eidolon_sdk.biz.events import DeviceEvent, normalize_device_event, parse_device_event
from livekit import api

from hub.config import AppConfig

logger = logging.getLogger(__name__)


class AmbientEventBusError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AmbientEventFanout:
    event: DeviceEvent
    owner_id: str
    recipient_device_ids: tuple[str, ...]


@dataclass(slots=True)
class _RateBucket:
    tokens: float
    updated_at: float


class AmbientEventBus:
    """Validate a publisher and broadcast its event to online owner devices.

    Event type never participates in recipient selection. Consumers decide
    locally whether to handle or ignore each event type.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        data_store: Any,
        runtime: Any,
        livekit_api_factory: Any | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._config = config
        self._data_store = data_store
        self._runtime = runtime
        self._livekit_api_factory = livekit_api_factory or self._build_livekit_api
        self._monotonic_clock = monotonic_clock
        self._recent: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._owner_rate_buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._recent_lock = asyncio.Lock()

    def _build_livekit_api(self) -> api.LiveKitAPI:
        cfg = self._config.livekit
        return api.LiveKitAPI(
            url=cfg.api_url,
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
        )

    async def handle_packet(
        self,
        *,
        topic: str,
        data: bytes,
        sender_identity: str,
    ) -> AmbientEventFanout | None:
        if topic != EVENT_TOPIC or not self._config.ambient_event_bus.enabled:
            return None
        if not sender_identity:
            raise AmbientEventBusError("device event publisher identity is required")
        if len(data) > self._config.ambient_event_bus.max_event_bytes:
            raise AmbientEventBusError("device event exceeds configured transport size")

        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AmbientEventBusError("device event must contain valid JSON") from exc

        now_ms = int(time() * 1_000)
        event = parse_device_event(payload, now_ms=now_ms)
        ttl_ms = event.expires_at_ms - event.occurred_at_ms
        if ttl_ms > self._config.ambient_event_bus.ttl_ms:
            raise AmbientEventBusError("device event exceeds configured TTL")

        publisher = await self._data_store.devices.get_device(sender_identity)
        owner_id = str(publisher.owner_id) if publisher is not None and publisher.owner_id else ""
        if not owner_id or str(publisher.status) != "active":
            raise AmbientEventBusError("device event publisher has no active owner binding")

        normalized = event.model_dump(mode="json")
        normalized["source"] = {
            "device_id": sender_identity,
            "component": event.source.component,
        }
        normalized = normalize_device_event(normalized, now_ms=now_ms)
        event = parse_device_event(normalized, now_ms=now_ms)

        dedup_key = (owner_id, event.event_id)
        if not await self._claim_event(dedup_key, owner_id):
            return AmbientEventFanout(event, owner_id, ())

        try:
            recipients_by_room = await self._online_recipients(owner_id)
            encoded = json.dumps(normalized, separators=(",", ":")).encode("utf-8")
            await self._fan_out(recipients_by_room, encoded)
        except Exception:
            await self._release_event(dedup_key)
            raise

        recipient_ids = tuple(
            sorted(
                device_id for device_ids in recipients_by_room.values() for device_id in device_ids
            )
        )
        logger.info(
            "Ambient event broadcast type=%s flow_id=%s source=%s recipients=%d",
            event.type,
            event.flow_id,
            sender_identity,
            len(recipient_ids),
        )
        return AmbientEventFanout(event, owner_id, recipient_ids)

    async def _online_recipients(self, owner_id: str) -> dict[str, list[str]]:
        owner_devices = await self._data_store.devices.list_devices_for_owner(owner_id)
        eligible = {str(row.device_id) for row in owner_devices if str(row.status) == "active"}
        presence = await self._runtime.get_presence_snapshot()
        recipients: dict[str, list[str]] = defaultdict(list)
        for item in presence:
            if item.device_id in eligible and item.status == "online" and item.room_name:
                recipients[item.room_name].append(item.device_id)
        return dict(recipients)

    async def _fan_out(
        self,
        recipients_by_room: dict[str, list[str]],
        encoded: bytes,
    ) -> None:
        if not recipients_by_room:
            return
        livekit_api = self._livekit_api_factory()
        try:
            for room_name, device_ids in sorted(recipients_by_room.items()):
                await livekit_api.room.send_data(
                    api.SendDataRequest(
                        room=room_name,
                        data=encoded,
                        kind=0,
                        destination_identities=sorted(device_ids),
                        topic=EVENT_TOPIC,
                    )
                )
        finally:
            await livekit_api.aclose()

    async def _claim_event(self, key: tuple[str, str], owner_id: str) -> bool:
        async with self._recent_lock:
            if key in self._recent:
                self._recent.move_to_end(key)
                return False
            if not self._consume_owner_rate(owner_id):
                raise AmbientEventBusError("owner device event rate limit exceeded")
            self._recent[key] = None
            while len(self._recent) > self._config.ambient_event_bus.recent_event_cache:
                self._recent.popitem(last=False)
            return True

    def _consume_owner_rate(self, owner_id: str) -> bool:
        rate = max(1, self._config.ambient_event_bus.owner_rate_per_second)
        burst = max(1, self._config.ambient_event_bus.owner_rate_burst)
        now = self._monotonic_clock()
        bucket = self._owner_rate_buckets.get(owner_id)
        if bucket is None:
            bucket = _RateBucket(tokens=float(burst), updated_at=now)
            self._owner_rate_buckets[owner_id] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(float(burst), bucket.tokens + elapsed * rate)
            bucket.updated_at = now
            self._owner_rate_buckets.move_to_end(owner_id)

        while len(self._owner_rate_buckets) > 4_096:
            self._owner_rate_buckets.popitem(last=False)
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    async def _release_event(self, key: tuple[str, str]) -> None:
        async with self._recent_lock:
            self._recent.pop(key, None)
