from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from livekit import api

from hub.config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class ProbeHealth:
    running: bool = False
    last_success_at: datetime | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    total_cycles: int = 0


@dataclass
class DevicePresence:
    device_id: str
    status: str = "offline"
    room_name: str = ""
    participant_sid: str = ""
    last_seen_at: datetime | None = None
    missed_probes: int = 0


class LiveKitAdminRuntime:
    def __init__(self, config: AppConfig):
        self._config = config
        self._state: dict[str, DevicePresence] = {}
        self._probe_health = ProbeHealth()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._commands: dict[str, dict[str, Any]] = {}
        self._command_order: list[str] = []

    def _build_livekit_api(self) -> api.LiveKitAPI:
        cfg = self._config.livekit
        url = cfg.url
        if not url:
            raise ValueError("LIVEKIT_API_URL is not configured")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(
                f"LIVEKIT_API_URL must be an http(s):// URL (got: {url!r}). "
                "This is the LiveKit server-side management API, not the client signaling WebSocket; "
                "use the client URL via EIDOLON_LIVEKIT_URL / EIDOLON_LIVEKIT_IP instead."
            )
        return api.LiveKitAPI(url=url, api_key=cfg.api_key, api_secret=cfg.api_secret)

    async def run_probe_cycle(self, known_device_ids: list[str]) -> None:
        self._probe_health.total_cycles += 1
        detected: dict[str, tuple[str, str]] = {}
        livekit_api = self._build_livekit_api()

        try:
            rooms = await livekit_api.room.list_rooms(api.ListRoomsRequest())
            for room in rooms.rooms:
                participants = await livekit_api.room.list_participants(
                    api.ListParticipantsRequest(room=room.name)
                )
                for participant in participants.participants:
                    detected[participant.identity] = (room.name, participant.sid)

            now = datetime.now(UTC)
            async with self._lock:
                for device_id in set(known_device_ids) | set(detected.keys()) | set(self._state.keys()):
                    current = self._state.get(device_id) or DevicePresence(device_id=device_id)
                    if device_id in detected:
                        room_name, participant_sid = detected[device_id]
                        status = "online"
                        current.status = status
                        current.room_name = room_name
                        current.participant_sid = participant_sid
                        current.last_seen_at = now
                        current.missed_probes = 0
                    else:
                        current.missed_probes += 1
                        if current.missed_probes >= self._config.admin.offline_after_missed_probes:
                            current.status = "offline"
                        elif current.missed_probes >= self._config.admin.degraded_after_missed_probes:
                            current.status = "degraded"
                    self._state[device_id] = current
            await self._emit_event({"type": "probe_cycle", "at": now.isoformat(), "detected": len(detected)})
            self._probe_health.last_success_at = now
            self._probe_health.consecutive_failures = 0
            self._probe_health.last_error = ""
        except Exception as exc:
            self._probe_health.consecutive_failures += 1
            self._probe_health.last_error = str(exc)
            async with self._lock:
                for device_id in set(known_device_ids) | set(self._state.keys()):
                    current = self._state.get(device_id) or DevicePresence(device_id=device_id)
                    current.status = "unknown"
                    current.missed_probes += 1
                    self._state[device_id] = current
            logger.warning("LiveKit probe cycle failed: %s", exc)
        finally:
            await livekit_api.aclose()

    async def get_presence_snapshot(self) -> list[DevicePresence]:
        async with self._lock:
            return list(self._state.values())

    def get_probe_health(self) -> ProbeHealth:
        return self._probe_health

    async def send_command(self, device_id: str, payload: dict[str, Any], topic: str) -> dict[str, Any]:
        async with self._lock:
            presence = self._state.get(device_id)
            if not presence or not presence.room_name:
                raise ValueError(f"Device {device_id} is not currently connected")

        command_id = str(uuid4())
        command = {
            "command_id": command_id,
            "device_id": device_id,
            "topic": topic,
            "payload": payload,
            "status": "queued",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "error": "",
        }
        message = json.dumps(
            {
                "type": "admin_command",
                "command_id": command_id,
                "device_id": device_id,
                "topic": topic,
                "payload": payload,
            }
        ).encode("utf-8")

        livekit_api = self._build_livekit_api()
        try:
            await livekit_api.room.send_data(
                api.SendDataRequest(
                    room=presence.room_name,
                    data=message,
                    kind=0,
                    destination_identities=[device_id],
                    topic=topic,
                )
            )
            command["status"] = "sent"
            command["updated_at"] = datetime.now(UTC).isoformat()
        except Exception as exc:
            command["status"] = "failed"
            command["error"] = str(exc)
            command["updated_at"] = datetime.now(UTC).isoformat()
        finally:
            await livekit_api.aclose()
        self._commands[command_id] = command
        self._command_order.append(command_id)
        await self._emit_event({"type": "command_updated", **command})
        if command["status"] == "failed":
            raise ValueError(command["error"])
        return command
        
    async def fail_command(
        self, command_id: str, error: str, status: str = "failed"
    ) -> dict[str, Any] | None:
        command = self._commands.get(command_id)
        if not command:
            return None
        command["status"] = status
        command["error"] = error
        command["updated_at"] = datetime.now(UTC).isoformat()
        await self._emit_event({"type": "command_updated", **command})
        return command

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        return self._commands.get(command_id)

    def list_commands(self, limit: int = 50) -> list[dict[str, Any]]:
        ids = self._command_order[-limit:]
        ids.reverse()
        return [self._commands[item] for item in ids if item in self._commands]

    async def mark_command_timeout(self, timeout_seconds: int) -> int:
        now = datetime.now(UTC)
        touched = 0
        for command in self._commands.values():
            if command["status"] != "sent":
                continue
            created_at = datetime.fromisoformat(command["created_at"])
            if (now - created_at).total_seconds() < timeout_seconds:
                continue
            command["status"] = "timeout"
            command["updated_at"] = now.isoformat()
            command["error"] = f"no ack/result within {timeout_seconds}s"
            touched += 1
            await self._emit_event({"type": "command_updated", **command})
        return touched

    async def get_metrics(self) -> dict[str, Any]:
        presence = await self.get_presence_snapshot()
        online = sum(1 for item in presence if item.status == "online")
        degraded = sum(1 for item in presence if item.status == "degraded")
        offline = sum(1 for item in presence if item.status == "offline")
        unknown = sum(1 for item in presence if item.status == "unknown")
        commands = list(self._commands.values())
        sent = sum(1 for item in commands if item["status"] == "sent")
        failed = sum(1 for item in commands if item["status"] == "failed")
        timeout = sum(1 for item in commands if item["status"] == "timeout")
        return {
            "probe": {
                "running": self._probe_health.running,
                "total_cycles": self._probe_health.total_cycles,
                "consecutive_failures": self._probe_health.consecutive_failures,
                "last_success_at": (
                    self._probe_health.last_success_at.isoformat()
                    if self._probe_health.last_success_at
                    else None
                ),
            },
            "devices": {
                "total": len(presence),
                "online": online,
                "degraded": degraded,
                "offline": offline,
                "unknown": unknown,
            },
            "commands": {
                "total": len(commands),
                "sent": sent,
                "failed": failed,
                "timeout": timeout,
            },
        }

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    async def _emit_event(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event)
        for queue in list(self._subscribers):
            if queue.full():
                continue
            await queue.put(payload)
