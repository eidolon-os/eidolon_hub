"""One Home Assistant WebSocket connection: auth, requests, a state cache, waits.

Small on purpose. The API surface the Provider needs is auth, ``get_states``,
``get_config``, ``subscribe_events`` for ``state_changed``, ``call_service`` and
the four registry/exposure listings; a client library would bring far more and
its own reconnect policy. Reconnects here are the Provider's: on loss the
connection is reopened with exponential backoff and the cache refilled from
``get_states``, so a reader never sees a partial cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp

logger = logging.getLogger("hub.smarthome.homeassistant")


class ConnectionFailed(Exception):
    """Could not reach or authenticate with the instance; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class HaConnection:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._states: dict[str, dict[str, Any]] = {}
        self._changed = asyncio.Condition()
        self._listeners: list[asyncio.Queue] = []
        self.ha_version = ""
        self.location_name = ""
        self._closed = False

    @property
    def states(self) -> dict[str, dict[str, Any]]:
        return self._states

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # --- lifecycle ------------------------------------------------------------

    async def open(self) -> None:
        ws_url = (
            self._url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            + "/api/websocket"
        )
        try:
            ws = await self._session.ws_connect(ws_url, heartbeat=30, max_msg_size=16 * 1024 * 1024)
        except (aiohttp.ClientError, OSError) as exc:
            raise ConnectionFailed("UNREACHABLE", f"cannot reach {self._url}: {exc}") from exc
        try:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                raise ConnectionFailed("NOT_HOME_ASSISTANT", "no auth_required greeting")
            await ws.send_json({"type": "auth", "access_token": self._token})
            answer = await ws.receive_json(timeout=10)
            if answer.get("type") != "auth_ok":
                raise ConnectionFailed(
                    "UNAUTHORIZED", answer.get("message") or "token was not accepted"
                )
            self.ha_version = str(answer.get("ha_version", ""))
        except (aiohttp.ClientError, asyncio.TimeoutError, TypeError) as exc:
            await ws.close()
            raise ConnectionFailed("UNREACHABLE", f"handshake failed: {exc}") from exc
        except ConnectionFailed:
            await ws.close()
            raise
        self._ws = ws
        self._reader = asyncio.create_task(self._read(ws), name=f"ha-read-{self._url}")
        config = await self.request({"type": "get_config"})
        self.location_name = str(config.get("location_name") or "Home Assistant")
        await self.refresh_states()
        await self.request({"type": "subscribe_events", "event_type": "state_changed"})

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionFailed("CLOSED", "connection closed"))
        self._pending.clear()

    async def refresh_states(self) -> None:
        states = await self.request({"type": "get_states"})
        self._states = {s["entity_id"]: s for s in states}
        async with self._changed:
            self._changed.notify_all()

    # --- requests -------------------------------------------------------------

    async def request(self, message: dict[str, Any], timeout: float = 15) -> Any:
        if self._ws is None or self._ws.closed:
            raise ConnectionFailed("CLOSED", "not connected")
        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._ws.send_json({"id": message_id, **message})
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(message_id, None)
        if not reply.get("success", False):
            error = reply.get("error") or {}
            raise ConnectionFailed(
                str(error.get("code") or "REQUEST_FAILED"),
                str(error.get("message") or "request failed"),
            )
        return reply.get("result")

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        target = {"entity_id": data.pop("entity_id")} if "entity_id" in data else {}
        await self.request(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "service_data": data,
                "target": target,
            }
        )

    async def registries(self) -> dict[str, Any]:
        areas, devices, entities, exposed = await asyncio.gather(
            self.request({"type": "config/area_registry/list"}),
            self.request({"type": "config/device_registry/list"}),
            self.request({"type": "config/entity_registry/list"}),
            self.request({"type": "homeassistant/expose_entity/list"}),
        )
        return {
            "areas": areas,
            "devices": devices,
            "entities": entities,
            "exposed": exposed.get("exposed_entities", {}),
        }

    async def expose(self, entity_ids: list[str], *, should_expose: bool = True) -> None:
        await self.request(
            {
                "type": "homeassistant/expose_entity",
                "assistants": ["conversation"],
                "entity_ids": entity_ids,
                "should_expose": should_expose,
            }
        )

    # --- state waits and change stream -------------------------------------------

    async def wait_state(
        self, entity_id: str, predicate: Callable[[dict[str, Any]], bool], timeout: float
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            current = self._states.get(entity_id)
            if current is not None and predicate(current):
                return current
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(entity_id)
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except TimeoutError:
                    current = self._states.get(entity_id)
                    if current is not None and predicate(current):
                        return current
                    raise

    def changes(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._listeners.append(queue)

        async def stream():
            try:
                while True:
                    yield await queue.get()
            finally:
                if queue in self._listeners:
                    self._listeners.remove(queue)

        return stream()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("type") == "event":
                    await self._on_event(payload.get("event") or {})
                    continue
                future = self._pending.get(payload.get("id"))
                if future is not None and not future.done():
                    future.set_result(payload)
        except Exception as exc:  # the Provider reconnects
            logger.warning("home assistant %s: reader stopped: %s", self._url, exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionFailed("CLOSED", "connection lost"))
            async with self._changed:
                self._changed.notify_all()

    async def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data") or {}
        new_state = data.get("new_state")
        entity_id = data.get("entity_id")
        if not entity_id:
            return
        if new_state is None:
            self._states.pop(entity_id, None)
        else:
            self._states[entity_id] = new_state
        async with self._changed:
            self._changed.notify_all()
        for queue in list(self._listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"entity_id": entity_id, "new_state": new_state})
