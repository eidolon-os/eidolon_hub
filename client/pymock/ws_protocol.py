"""WebSocket client with pipeline protocol for the GUI client."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

try:
    import websockets
except ImportError:
    websockets = None


class ClientState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LISTENING = "listening"
    RECEIVING = "receiving"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass
class ServerMessage:
    type: str
    payload: dict
    raw: dict


class WSProtocolClient:
    """WebSocket client for the pipeline server with protocol handling.

    Protocol flow (AUTO mode):
      1. connect()  -> server sends 'connected'
      2. auto handshake sent
      3. server sends 'handshake {state: ok}'
      4. send_listen_start() -> server sends 'listening_ok'
      5. start audio send loop + recording
      6. server sends binary Opus, stt, agent, ducking, etc.
      7. send_listen_stop() -> server sends 'listening_stopped'
    """

    def __init__(
        self,
        host: str,
        port: int,
        path: str = "/auto",
        device_id: str = "gui-client",
        client_id: str = "gui-client-001",
    ):
        if websockets is None:
            raise RuntimeError("websockets not installed")
        self.host = host
        self.port = port
        self.path = path
        self.device_id = device_id
        self.client_id = client_id

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._state = ClientState.DISCONNECTED
        self._session_id: Optional[str] = None
        self._listening = False
        self._connected = False

        self._on_state_change: Optional[Callable[[ClientState], None]] = None
        self._on_server_message: Optional[Callable[[ServerMessage], None]] = None
        self._on_binary_frame: Optional[Callable[[bytes], None]] = None
        self._on_log: Optional[Callable[[str], None]] = None

        self._audio_queue: queue.Queue[bytes] = queue.Queue()

    def _is_ws_closed(self) -> bool:
        """Check if WebSocket is closed (compatible with websockets 15+)."""
        if self._ws is None:
            return True
        ws = self._ws
        if hasattr(ws, "state"):
            return ws.state.name == "CLOSED"
        if hasattr(ws, "closed"):
            return ws.closed
        return True

    # ---- public API ----

    def set_state_callback(self, cb: Callable[[ClientState], None]) -> None:
        self._on_state_change = cb

    def set_message_callback(self, cb: Callable[[ServerMessage], None]) -> None:
        self._on_server_message = cb

    def set_binary_callback(self, cb: Callable[[bytes], None]) -> None:
        self._on_binary_frame = cb

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._on_log = cb

    def send_audio_frame(self, frame: bytes) -> None:
        """Called from audio callback to enqueue a frame for sending."""
        self._audio_queue.put(frame)

    @property
    def state(self) -> ClientState:
        return self._state

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def connect(self) -> None:
        if self._thread is not None:
            return
        self._set_state(ClientState.CONNECTING)
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._do_disconnect(), self._loop)
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._loop = None
        self._set_state(ClientState.DISCONNECTED)

    def send_handshake(self) -> None:
        self._send_json({
            "type": "handshake",
            "version": "v1",
            "transport": "websocket",
            "payload": {},
        })

    def send_listen_start(self, mode: str = "auto") -> None:
        """Start listening. In AUTO mode: listen { mode: auto, state: start }."""
        self._send_json({
            "type": "listen",
            "version": "v1",
            "transport": "websocket",
            "payload": {"mode": mode, "state": "start"},
        })

    def send_listen_stop(self) -> None:
        """Stop listening."""
        self._send_json({
            "type": "listen",
            "version": "v1",
            "transport": "websocket",
            "payload": {"state": "stop"},
        })

    def send_start_listening(self) -> None:
        """Deprecated: use send_listen_start(). Start sending audio (AUTO mode)."""
        self._send_json({
            "type": "start_listening",
            "version": "v1",
            "transport": "websocket",
            "payload": {},
        })

    def send_stop_listening(self) -> None:
        """Deprecated: use send_listen_stop(). Stop sending audio (AUTO mode)."""
        self._send_json({
            "type": "stop_listening",
            "version": "v1",
            "transport": "websocket",
            "payload": {},
        })

    def send_state(self, state: str) -> None:
        """Report client state: idle / recording / speaking."""
        self._send_json({
            "type": "state",
            "version": "v1",
            "transport": "websocket",
            "payload": {"state": state},
        })

    def send_ping(self) -> None:
        """Send a ping heartbeat."""
        self._send_json({
            "type": "ping",
            "version": "v1",
            "transport": "websocket",
            "payload": {"timestamp": time.time()},
        }, log=False)

    # ---- internal ----

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._do_connect())

    async def _do_connect(self) -> None:
        uri = f"ws://{self.host}:{self.port}{self.path}"
        headers = websockets.Headers()
        headers["Device-ID"] = self.device_id
        headers["Client-ID"] = self.client_id
        try:
            self._ws = await websockets.connect(uri, additional_headers=headers)
            self._connected = True
            self._log(f"Connected to {uri}")
            asyncio.create_task(self._send_loop())
            await self._receive_loop()
        except Exception as e:
            self._log(f"Connection error: {e}")
            self._set_state(ClientState.ERROR)

    async def _do_disconnect(self) -> None:
        try:
            if self._ws and not self._is_ws_closed():
                await self._ws.close()
        except Exception:
            pass
        self._connected = False
        self._listening = False

    async def _receive_loop(self) -> None:
        if self._ws is None:
            return
        while True:
            try:
                msg = await self._ws.recv()
                if isinstance(msg, str):
                    await self._handle_text_message(msg)
                elif isinstance(msg, bytes):
                    self._handle_binary_message(msg)

                if self._connected and time.time() - self._last_ping_time > self._ping_interval:
                    self.send_ping()
                    self._last_ping_time = time.time()
            except websockets.exceptions.ConnectionClosed:
                self._log("Connection closed by server")
                break
            except Exception:
                break

    async def _send_loop(self) -> None:
        """Pump audio frames to server while listening."""
        if self._ws is None:
            return
        while True:
            try:
                frame = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._connected or self._is_ws_closed():
                    break
                continue
            try:
                if not self._is_ws_closed():
                    await self._ws.send(frame)
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception:
                break

    async def _handle_text_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._log(f"Invalid JSON: {raw[:100]}")
            return

        msg_type = data.get("type", "?")
        payload = data.get("payload", {})
        msg = ServerMessage(type=msg_type, payload=payload, raw=data)

        self._log(f"<- {msg_type}: {payload}")

        if msg_type == "connected":
            self._set_state(ClientState.CONNECTED)
            self.send_handshake()

        elif msg_type == "handshake":
            state = payload.get("state", "")
            if state == "ok":
                self._log(f"Handshake OK, mode={payload.get('mode')}")

        elif msg_type == "listening_ok":
            self._session_id = payload.get("session_id")
            self._listening = True
            self._set_state(ClientState.LISTENING)
            self.send_state("recording")

        elif msg_type == "listening_stopped":
            self._listening = False
            self._set_state(ClientState.CONNECTED)
            self.send_state("idle")

        elif msg_type == "start_listening":
            pass

        elif msg_type == "stop_listening":
            pass

        elif msg_type == "session_start":
            self._session_id = payload.get("session_id")
            self._set_state(ClientState.SPEAKING)
            self.send_state("speaking")

        elif msg_type == "session_end":
            self._session_id = None
            self._set_state(ClientState.CONNECTED)
            self.send_state("idle")

        elif msg_type == "stop_playing":
            if self._on_binary_frame:
                self._on_binary_frame(b"")

        elif msg_type == "ducking":
            vol = payload.get("volume", 1.0)
            self._log(f"[ducking] volume={vol}")

        elif msg_type == "pong":
            ts = payload.get("timestamp")
            st = payload.get("server_time")
            if ts and st:
                rtt = (time.time() - ts) * 1000
                self._log(f"[pong] RTT={rtt:.0f}ms, server_time={st}")

        elif msg_type == "error":
            self._log(f"[error] {payload.get('code')}: {payload.get('message', '')}")

        elif msg_type == "close":
            self._log(f"[close] reason={payload.get('reason')}, code={payload.get('code')}")
            self._connected = False
            self._set_state(ClientState.ERROR)

        if self._on_server_message:
            self._on_server_message(msg)

    def _handle_binary_message(self, data: bytes) -> None:
        if self._on_binary_frame:
            self._on_binary_frame(data)

    def _send_json(self, data: dict, log: bool = True) -> None:
        """Schedule a JSON text message to be sent."""
        if self._loop is None or self._ws is None or self._is_ws_closed():
            return
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(data)), self._loop)
        if log:
            self._log(f"-> {data['type']}")

    def _set_state(self, state: ClientState) -> None:
        self._state = state
        if self._on_state_change:
            self._on_state_change(state)

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)
