from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from typing import Any

from eidolon_sdk.control import CONTROL_TOPIC
from livekit import api

from hub.config import AppConfig
from hub.core.admin_runtime import LiveKitAdminRuntime

logger = logging.getLogger(__name__)


def _bridge_url(config: AppConfig) -> str:
    explicit = config.control_bridge.livekit_url or config.esp32.livekit_url
    if explicit:
        return explicit
    api_url = config.livekit.api_url
    if api_url.startswith("https://"):
        return "wss://" + api_url.removeprefix("https://")
    if api_url.startswith("http://"):
        return "ws://" + api_url.removeprefix("http://")
    return api_url


def _identity(prefix: str, room_name: str) -> str:
    digest = hashlib.sha1(room_name.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


class LiveKitControlBridge:
    """Join LiveKit rooms as a lightweight participant and consume control acks.

    LiveKit's server API can inject data packets into rooms, but it does not
    subscribe to packets published by clients. This bridge is the small missing
    participant that listens for ``eidolon.control`` ack/result envelopes and
    forwards them into ``LiveKitAdminRuntime``.
    """

    def __init__(self, config: AppConfig, runtime: LiveKitAdminRuntime):
        self._config = config
        self._runtime = runtime
        self._rooms: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._rtc: Any | None = None

    async def start(self) -> None:
        if not self._config.control_bridge.enabled:
            return
        try:
            from livekit import rtc  # type: ignore
        except Exception as exc:
            logger.warning(
                "LiveKit control bridge disabled: livekit.rtc is unavailable (%s)",
                exc,
            )
            return
        if not _bridge_url(self._config):
            logger.warning("LiveKit control bridge disabled: no LiveKit client URL configured")
            return
        self._rtc = rtc
        self._started = True
        logger.info("LiveKit control bridge enabled")

    async def stop(self) -> None:
        self._started = False
        async with self._lock:
            rooms = list(self._rooms.values())
            self._rooms.clear()
        for room in rooms:
            with suppress(Exception):
                await room.disconnect()

    async def sync_rooms(self, room_names: list[str]) -> None:
        if not self._started:
            return
        for room_name in sorted({item for item in room_names if item}):
            await self.ensure_room(room_name)

    async def ensure_room(self, room_name: str) -> None:
        if not self._started or not self._rtc or not room_name:
            return
        async with self._lock:
            if room_name in self._rooms:
                return
            room = self._rtc.Room()
            self._install_handler(room)
            self._rooms[room_name] = room

        token = self._token(room_name)
        try:
            await room.connect(_bridge_url(self._config), token)
            logger.info("LiveKit control bridge joined room=%s", room_name)
        except Exception:
            async with self._lock:
                self._rooms.pop(room_name, None)
            logger.exception("LiveKit control bridge failed to join room=%s", room_name)

    def _token(self, room_name: str) -> str:
        cfg = self._config.livekit
        identity = _identity(self._config.control_bridge.identity_prefix, room_name)
        return (
            api.AccessToken(cfg.api_key, cfg.api_secret)
            .with_identity(identity)
            .with_name(identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=False,
                    can_subscribe=True,
                    can_publish_data=False,
                )
            )
            .to_jwt()
        )

    def _install_handler(self, room: Any) -> None:
        @room.on("data_received")
        def _on_data_received(packet: Any) -> None:
            asyncio.create_task(self._handle_packet(packet))

    async def _handle_packet(self, packet: Any) -> None:
        if getattr(packet, "topic", None) != CONTROL_TOPIC:
            return
        data = getattr(packet, "data", b"") or b""
        if isinstance(data, str):
            raw = data.encode("utf-8")
        else:
            raw = bytes(data)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            logger.debug("LiveKit control bridge ignored malformed JSON", exc_info=True)
            return
        if envelope.get("kind") not in {"ack", "result"}:
            return

        participant = getattr(packet, "participant", None)
        sender_identity = getattr(participant, "identity", "") or ""
        updated = await self._runtime.apply_command_ack(
            envelope,
            sender_identity=sender_identity,
        )
        if updated is None:
            logger.debug(
                "LiveKit control bridge ignored ack sender=%s ref=%s",
                sender_identity,
                envelope.get("ref") or envelope.get("command_id"),
            )

