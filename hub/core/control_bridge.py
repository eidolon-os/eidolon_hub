from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from typing import Any

from eidolon_sdk.biz.contracts import (
    CONTROL_OP_PLAYBACK_STOP,
    CONTROL_OP_PTT_TURN_STATUS,
    CONTROL_TOPIC,
    LIVEKIT_AGENT_SESSION_TOPIC,
    LIVEKIT_TRANSCRIPTION_TOPIC,
)
from eidolon_sdk.integrations.livekit import build_livekit_token

from hub.config import AppConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.guard_ingress import GuardIngress
from hub.core.guard_policy import GuardControlPlane, GuardPolicyError

logger = logging.getLogger(__name__)

_SESSION_LOCAL_ACK_REF_PREFIXES = (
    f"{CONTROL_OP_PLAYBACK_STOP}:",
    f"{CONTROL_OP_PTT_TURN_STATUS}:",
)


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

    def __init__(
        self,
        config: AppConfig,
        runtime: LiveKitAdminRuntime,
        guard_control_plane: GuardControlPlane | None = None,
        guard_ingress: GuardIngress | None = None,
        guard_runtime_reconciler: Any | None = None,
        guard_body_delivery: Any | None = None,
    ):
        self._config = config
        self._runtime = runtime
        self._guard_ingress = guard_ingress
        if self._guard_ingress is None and guard_control_plane is not None:
            self._guard_ingress = GuardIngress(guard_control_plane)
        self._guard_runtime_reconciler = guard_runtime_reconciler
        self._guard_body_delivery = guard_body_delivery
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
        """Reconcile the bridge's joined rooms to exactly ``room_names``.

        Joins rooms we're missing AND leaves rooms no longer in the desired set
        (plan §4.2.5 / I3): when a device goes offline the caller drops its
        control room from the list, so the bridge participant disconnects and the
        now-empty control room is reclaimed by LiveKit's empty_timeout. Previously
        this only ever added rooms, so an offline device's control room was kept
        alive forever by the lingering bridge participant.
        """
        if not self._started:
            return
        desired = {item for item in room_names if item}
        async with self._lock:
            current = set(self._rooms.keys())
        for room_name in sorted(desired - current):
            await self.ensure_room(room_name)
        for room_name in sorted(current - desired):
            await self.leave_room(room_name)

    async def leave_room(self, room_name: str) -> None:
        """Disconnect the bridge from one room (releasing it for reclamation)."""
        async with self._lock:
            room = self._rooms.pop(room_name, None)
        if room is None:
            return
        with suppress(Exception):
            await room.disconnect()
        logger.info("LiveKit control bridge left room=%s (reclaimed)", room_name)

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
        return build_livekit_token(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            room_name=room_name,
            identity=identity,
            name=identity,
            dispatch_agent=False,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=False,
        )

    def _install_handler(self, room: Any) -> None:
        @room.on("data_received")
        def _on_data_received(packet: Any) -> None:
            asyncio.create_task(self._handle_packet(packet))

        register_text = getattr(room, "register_text_stream_handler", None)
        if callable(register_text):
            with suppress(ValueError):
                register_text(LIVEKIT_TRANSCRIPTION_TOPIC, self._handle_text_stream)

        register_byte = getattr(room, "register_byte_stream_handler", None)
        if callable(register_byte):
            with suppress(ValueError):
                register_byte(LIVEKIT_AGENT_SESSION_TOPIC, self._handle_byte_stream)

    def _handle_text_stream(self, reader: Any, participant_identity: str) -> None:
        asyncio.create_task(
            self._drain_stream(reader, participant_identity=participant_identity)
        )

    def _handle_byte_stream(self, reader: Any, participant_identity: str) -> None:
        asyncio.create_task(
            self._drain_stream(reader, participant_identity=participant_identity)
        )

    async def _drain_stream(self, reader: Any, *, participant_identity: str) -> None:
        topic = getattr(getattr(reader, "info", None), "topic", "")
        try:
            async for _ in reader:
                pass
        except Exception:
            logger.debug(
                "LiveKit control bridge stream drain failed topic=%s participant=%s",
                topic,
                participant_identity,
                exc_info=True,
            )

    async def _handle_packet(self, packet: Any) -> None:
        if getattr(packet, "topic", None) != CONTROL_TOPIC:
            return
        data = getattr(packet, "data", b"") or b""
        if isinstance(data, str):
            raw = data.encode("utf-8")
        else:
            raw = bytes(data)
        participant = getattr(packet, "participant", None)
        sender_identity = getattr(participant, "identity", "") or ""
        if self._guard_ingress is not None:
            try:
                accepted = await self._guard_ingress.handle_packet(
                    topic=CONTROL_TOPIC,
                    data=raw,
                    sender_identity=sender_identity,
                    source="livekit",
                )
            except (GuardPolicyError, ValueError):
                logger.warning("LiveKit control bridge rejected guard event sender=%s", sender_identity)
                return
            if accepted is not None:
                return
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            logger.debug("LiveKit control bridge ignored malformed JSON", exc_info=True)
            return
        if envelope.get("kind") not in {"ack", "result"}:
            return

        updated = await self._runtime.apply_command_ack(
            envelope,
            sender_identity=sender_identity,
        )
        if updated is not None and self._guard_runtime_reconciler is not None:
            await self._guard_runtime_reconciler.apply_command_result(updated)
        if updated is not None and self._guard_body_delivery is not None:
            await self._guard_body_delivery.apply_command_result(updated)
        if updated is None:
            ref = envelope.get("ref") or envelope.get("command_id")
            if _is_session_local_ack_ref(ref):
                logger.debug(
                    "LiveKit control bridge observed session-local ack sender=%s ref=%s",
                    sender_identity,
                    ref,
                )
                return
            logger.debug(
                "LiveKit control bridge ignored unknown ack sender=%s ref=%s",
                sender_identity,
                ref,
            )


def _is_session_local_ack_ref(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith(_SESSION_LOCAL_ACK_REF_PREFIXES)
