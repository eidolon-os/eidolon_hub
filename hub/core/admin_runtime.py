from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from eidolon_sdk.biz.contracts import CONTROL_TOPIC
from eidolon_sdk.biz.control import (
    CommandPriority,
    CommandQoS,
    build_command_envelope,
    command_status_from_ack,
    infer_op,
    normalize_ack_status,
)
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
    def __init__(self, config: AppConfig, data_store: Any | None = None):
        self._config = config
        self._data_store = data_store
        self._state: dict[str, DevicePresence] = {}
        self._probe_health = ProbeHealth()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._commands: dict[str, dict[str, Any]] = {}
        self._command_order: list[str] = []
        self._control_bridge: Any | None = None

    def set_control_bridge(self, bridge: Any | None) -> None:
        self._control_bridge = bridge

    def _build_livekit_api(self) -> api.LiveKitAPI:
        cfg = self._config.livekit
        url = cfg.api_url
        if not url:
            raise ValueError("livekit.api_url is not configured in settings.yaml")
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
        known = set(known_device_ids)
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
                next_state: dict[str, DevicePresence] = {}
                for device_id in known:
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
                            current.room_name = ""
                            current.participant_sid = ""
                        elif current.missed_probes >= self._config.admin.degraded_after_missed_probes:
                            current.status = "degraded"
                    next_state[device_id] = current
                self._state = next_state
            await self._emit_event(
                {
                    "type": "probe_cycle",
                    "at": now.isoformat(),
                    "detected": len(set(detected.keys()) & known),
                    "ignored": len(set(detected.keys()) - known),
                }
            )
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
                    current.room_name = ""
                    current.participant_sid = ""
                    current.missed_probes += 1
                    self._state[device_id] = current
            logger.warning("LiveKit probe cycle failed: %s", exc)
        finally:
            await livekit_api.aclose()

    async def get_presence_snapshot(self) -> list[DevicePresence]:
        async with self._lock:
            return list(self._state.values())

    async def forget_presence(self, device_id: str) -> bool:
        """Drop the presence cache entry for a device. Returns True if one
        was present. Idempotent.

        Used by the unregister-device flow: after the device is gone from
        ``device_manager``, presence-cached state would otherwise linger
        until the next probe cycle, briefly showing a ghost row in admin
        UI. Cleaning here keeps the view consistent.

        Note: if the device is still physically connected to LiveKit,
        the next probe will repopulate the cache; that's correct — the
        device will then re-appear in admin as a *new* unapproved record
        (because device_manager forgot the persistent approval state).
        """
        async with self._lock:
            if device_id not in self._state:
                return False
            del self._state[device_id]
        return True

    def get_probe_health(self) -> ProbeHealth:
        return self._probe_health

    async def send_command(
        self,
        device_id: str,
        payload: dict[str, Any],
        topic: str = CONTROL_TOPIC,
        *,
        source_device_id: str | None = None,
        runtime_caller_id: str | None = None,
        runtime_session_id: str | None = None,
        op: str | None = None,
        ttl_ms: int = 30_000,
        qos: CommandQoS = "ack",
        priority: CommandPriority = "normal",
    ) -> dict[str, Any]:
        async with self._lock:
            presence = self._state.get(device_id)
            if not presence or not presence.room_name or presence.status != "online":
                raise ValueError(f"Device {device_id} is not currently connected")
            room_name = presence.room_name

        if self._control_bridge is not None:
            await self._control_bridge.ensure_room(room_name)

        command_id = str(uuid4())
        now = datetime.now(UTC)
        resolved_op = infer_op(payload, op)
        envelope = build_command_envelope(
            command_id=command_id,
            device_id=device_id,
            payload=payload,
            op=resolved_op,
            ttl_ms=ttl_ms,
            qos=qos,
            priority=priority,
            created_at=now,
        )
        command = {
            "command_id": command_id,
            "device_id": device_id,
            "runtime_caller_id": runtime_caller_id,
            "runtime_session_id": runtime_session_id,
            "source_device_id": source_device_id,
            "topic": topic,
            "op": resolved_op,
            "payload": payload,
            "envelope": envelope,
            "ttl_ms": ttl_ms,
            "qos": qos,
            "priority": priority,
            "status": "queued",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "error": "",
            "ack": None,
            "result": None,
        }
        await self._hydrate_command_binding(command)
        self._commands[command_id] = command
        self._command_order.append(command_id)
        await self._persist_command(command)
        message = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        livekit_api = self._build_livekit_api()
        try:
            await livekit_api.room.send_data(
                api.SendDataRequest(
                    room=room_name,
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
        await self._persist_command(command)
        await self._emit_event({"type": "command_updated", **command})
        if command["status"] == "failed":
            raise ValueError(command["error"])
        return command

    async def apply_command_ack(
        self,
        envelope: dict[str, Any],
        *,
        sender_identity: str = "",
    ) -> dict[str, Any] | None:
        """Apply a device ack/result envelope.

        LiveKit server API can inject packets into a room, but receiving
        device-published packets requires a participant bridge. This method
        keeps status normalization in one place for that bridge or a fallback
        HTTP endpoint.
        """
        command_id = envelope.get("ref") or envelope.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            return None
        command = self._commands.get(command_id)
        if not command:
            return None
        ack_device_id = envelope.get("device_id")
        if isinstance(ack_device_id, str) and ack_device_id and ack_device_id != command["device_id"]:
            logger.warning(
                "Ignoring command ack with mismatched device_id command_id=%s expected=%s got=%s",
                command_id,
                command["device_id"],
                ack_device_id,
            )
            return None
        if sender_identity and sender_identity != command["device_id"]:
            logger.warning(
                "Ignoring command ack with mismatched sender command_id=%s expected=%s got=%s",
                command_id,
                command["device_id"],
                sender_identity,
            )
            return None

        status_value = envelope.get("status")
        status = normalize_ack_status(status_value if isinstance(status_value, str) else "failed")
        command["status"] = command_status_from_ack(status)
        command["ack"] = envelope
        if envelope.get("kind") == "result" or "result" in envelope:
            command["result"] = envelope.get("result", envelope)
        if command["status"] in {"failed", "rejected", "expired"}:
            message = envelope.get("message") or envelope.get("code") or command["status"]
            command["error"] = str(message)
        command["updated_at"] = datetime.now(UTC).isoformat()
        await self._persist_command(command)
        await self._emit_event({"type": "command_updated", **command})
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
        await self._persist_command(command)
        await self._emit_event({"type": "command_updated", **command})
        return command

    async def get_command(self, command_id: str) -> dict[str, Any] | None:
        command = self._commands.get(command_id)
        if command is not None:
            return command
        if self._data_store is None:
            return None
        row = await self._data_store.body_commands.get_command(command_id)
        if row is None:
            return None
        return _command_from_row(row)

    async def list_commands(self, limit: int = 50) -> list[dict[str, Any]]:
        ids = self._command_order[-limit:]
        ids.reverse()
        commands = [self._commands[item] for item in ids if item in self._commands]
        if self._data_store is None or len(commands) >= limit:
            return commands[:limit]

        seen = {item["command_id"] for item in commands}
        rows = await self._data_store.body_commands.list_recent(limit=limit)
        for row in rows:
            if row.command_id in seen:
                continue
            commands.append(_command_from_row(row))
            seen.add(row.command_id)
            if len(commands) >= limit:
                break
        return commands

    async def mark_command_timeout(self, timeout_seconds: int) -> int:
        now = datetime.now(UTC)
        touched = 0
        for command in self._commands.values():
            if command["status"] not in {"sent", "accepted", "running"}:
                continue
            created_at = datetime.fromisoformat(command["created_at"])
            if (now - created_at).total_seconds() < timeout_seconds:
                continue
            command["status"] = "timeout"
            command["updated_at"] = now.isoformat()
            command["error"] = f"no ack/result within {timeout_seconds}s"
            touched += 1
            await self._persist_command(command)
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

    async def _hydrate_command_binding(self, command: dict[str, Any]) -> None:
        if self._data_store is None:
            return
        if command.get("owner_id") is not None:
            return
        try:
            row = await self._data_store.devices.get_device(command["device_id"])
        except Exception:
            logger.exception(
                "Failed to load device binding for body command command_id=%s device_id=%s",
                command.get("command_id"),
                command.get("device_id"),
            )
            return
        if row is None:
            return
        command["owner_id"] = row.owner_id
        command["companion_id"] = row.bound_companion_id

    async def _persist_command(self, command: dict[str, Any]) -> None:
        if self._data_store is None:
            return
        try:
            await self._data_store.body_commands.upsert_command(
                command_id=command["command_id"],
                owner_id=command.get("owner_id"),
                companion_id=command.get("companion_id"),
                runtime_caller_id=command.get("runtime_caller_id"),
                runtime_session_id=command.get("runtime_session_id"),
                device_id=command["device_id"],
                source_device_id=command.get("source_device_id"),
                topic=command.get("topic") or "",
                op=command.get("op") or "",
                status=command.get("status") or "queued",
                payload_json=command.get("payload") or {},
                envelope_json=command.get("envelope") or {},
                ack_json=command.get("ack"),
                result_json=_json_dict_or_none(command.get("result")),
                ttl_ms=int(command.get("ttl_ms") or 30_000),
                qos=str(command.get("qos") or "ack"),
                priority=str(command.get("priority") or "normal"),
                error=str(command.get("error") or ""),
                created_at=_parse_datetime(command.get("created_at")),
                updated_at=_parse_datetime(command.get("updated_at")),
                expires_at=_command_expires_at(command),
            )
        except Exception:
            logger.exception(
                "Failed to persist body command command_id=%s device_id=%s",
                command.get("command_id"),
                command.get("device_id"),
            )


def _command_from_row(row: Any) -> dict[str, Any]:
    return {
        "command_id": row.command_id,
        "owner_id": row.owner_id,
        "companion_id": row.companion_id,
        "device_id": row.device_id,
        "runtime_caller_id": row.runtime_caller_id,
        "runtime_session_id": row.runtime_session_id,
        "source_device_id": row.source_device_id,
        "topic": row.topic,
        "op": row.op,
        "payload": row.payload_json or {},
        "envelope": row.envelope_json or {},
        "ttl_ms": row.ttl_ms,
        "qos": row.qos,
        "priority": row.priority,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "error": row.error or "",
        "ack": row.ack_json,
        "result": row.result_json,
    }


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _command_expires_at(command: dict[str, Any]) -> datetime | None:
    created_at = _parse_datetime(command.get("created_at"))
    if created_at is None:
        return None
    try:
        ttl_ms = int(command.get("ttl_ms") or 0)
    except (TypeError, ValueError):
        return None
    if ttl_ms <= 0:
        return None
    return created_at + timedelta(milliseconds=ttl_ms)


def _json_dict_or_none(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": value}
