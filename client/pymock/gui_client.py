"""GUI client for the pipeline WebSocket server.

Usage::

    python -m client.pymock.gui_client

Or::

    python -m client.pymock
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .audio import AudioEngine
from .constants import (
    AUDIO_FORMATS,
    DEFAULT_WS_HOST,
    DEFAULT_WS_PORT,
    DEFAULT_WS_PATH,
    FORMAT_OPUS,
    FORMAT_PCM,
    SAMPLE_RATE,
    CHANNELS,
    AUDIO_FRAME_SIZE,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    LOG_TEXT_HEIGHT,
    STATUS_COLOR_CONNECTED,
    STATUS_COLOR_ERROR,
    STATUS_COLOR_IDLE,
    STATUS_COLOR_LISTENING,
    STATUS_COLOR_SPEAKING,
)
from .ui_components import ControlPanel, LogPanel, StatusBar
from .ws_protocol import ClientState, ServerMessage, WSProtocolClient


class GuiClient(tk.Tk):
    """Main GUI application."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Eidolon GUI Client")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")

        # ---- Audio format selector ----
        fmt_frame = ttk.Frame(self, padding=(8, 4))
        fmt_frame.pack(fill=tk.X)
        ttk.Label(fmt_frame, text="音频格式:").pack(side=tk.LEFT, padx=(0, 4))
        self._fmt_var = tk.StringVar(value=FORMAT_OPUS)
        for fmt in AUDIO_FORMATS:
            ttk.Radiobutton(
                fmt_frame,
                text=fmt.upper(),
                variable=self._fmt_var,
                value=fmt,
                command=self._on_format_change,
            ).pack(side=tk.LEFT, padx=4)

        # ---- Status bar ----
        self._status = StatusBar(self)
        self._status.pack(fill=tk.X)
        self._status.set_connection("未连接", STATUS_COLOR_IDLE)
        self._status.set_recording("停止", STATUS_COLOR_IDLE)
        self._status.set_format(FORMAT_OPUS.upper())

        # ---- Control buttons ----
        self._controls = ControlPanel(
            self,
            on_connect=self._on_connect,
            on_start_listening=self._on_start_listening,
            on_stop_listening=self._on_stop_listening,
            on_auto_chat=self._on_auto_chat,
        )
        self._controls.pack(fill=tk.X)

        # ---- Log panel ----
        self._log_panel = LogPanel(self, height=LOG_TEXT_HEIGHT)
        self._log_panel.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ---- Engine objects ----
        self._audio: AudioEngine | None = None
        self._ws: WSProtocolClient | None = None
        self._audio_format = FORMAT_OPUS
        self._auto_recording = False
        self._session_active = False  # True between listen start and listen stop

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_format_change(self) -> None:
        fmt = self._fmt_var.get()
        self._audio_format = fmt
        self._status.set_format(fmt.upper())

    def _on_connect(self) -> None:
        ws = self._ws
        if ws is not None and ws.state not in (ClientState.DISCONNECTED, ClientState.ERROR):
            self._log("Disconnecting...")
            ws.disconnect()
            self._ws = None
            self._audio = None
            self._status.set_connection("未连接", STATUS_COLOR_IDLE)
            self._controls.set_connected(False)
            self._controls.set_recording(False)
            self._auto_recording = False
            self._session_active = False
            return

        host = DEFAULT_WS_HOST
        port = DEFAULT_WS_PORT
        path = DEFAULT_WS_PATH

        self._log(f"Connecting to ws://{host}:{port}{path}...")
        self._status.set_connection("连接中...", STATUS_COLOR_IDLE)

        self._audio = AudioEngine(
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            frame_size=AUDIO_FRAME_SIZE,
            use_opus=(self._audio_format == FORMAT_OPUS),
        )

        self._ws = WSProtocolClient(host=host, port=port, path=path)
        self._ws.set_state_callback(self._on_state_change)
        self._ws.set_message_callback(self._on_server_message)
        self._ws.set_binary_callback(self._on_binary_frame)
        self._ws.set_log_callback(self._on_ws_log)

        self._audio.set_frame_callback(self._ws.send_audio_frame)

        self._ws.connect()

    def _on_start_listening(self) -> None:
        ws = self._ws
        audio = self._audio
        if ws is None or ws.state not in (ClientState.CONNECTED, ClientState.LISTENING):
            self._log("请先连接服务器")
            return
        if self._auto_recording:
            self._log("已在录音中")
            return

        self._log("Starting listening (listen {mode: auto, state: start})...")
        # Enable stop button immediately; server response will update state if needed.
        self._session_active = True
        self._auto_recording = True
        self._controls.set_listening_active(True)
        self._status.set_recording("录音中", STATUS_COLOR_LISTENING)
        if audio:
            try:
                audio.start_recording()
            except RuntimeError as e:
                self._log(f"Recording error: {e}")
                return
        ws.send_listen_start(mode="auto")

    def _on_stop_listening(self) -> None:
        ws = self._ws
        audio = self._audio
        if ws is None:
            return

        self._log("Stopping listening (listen {state: stop})...")
        ws.send_listen_stop()

        if audio:
            audio.stop_recording()
        self._auto_recording = False
        self._session_active = False
        self._status.set_recording("停止", STATUS_COLOR_IDLE)
        self._controls.set_listening_active(False)

    def _on_auto_chat(self) -> None:
        ws = self._ws
        audio = self._audio
        if ws is None or audio is None:
            self._log("请先连接服务器")
            return
        if ws.state != ClientState.CONNECTED:
            self._log("请先完成连接")
            return
        if self._auto_recording:
            self._log("已在录音中")
            return

        self._log("Starting auto chat (listen {mode: auto, state: start})...")
        ws.send_listen_start(mode="auto")

    def _on_state_change(self, state: ClientState) -> None:
        self.after(0, self._apply_state, state)

    def _apply_state(self, state: ClientState) -> None:
        ws = self._ws
        audio = self._audio

        if state == ClientState.CONNECTED:
            self._status.set_connection("已连接", STATUS_COLOR_CONNECTED)
            self._controls.set_connected(True)
            # Reset listening controls after a session ends naturally
            if self._session_active:
                self._session_active = False
                self._auto_recording = False
                self._controls.set_listening_active(False)
                if audio:
                    audio.stop_recording()

        elif state == ClientState.LISTENING:
            self._status.set_recording("录音中", STATUS_COLOR_LISTENING)
            self._session_active = True
            self._auto_recording = True
            self._controls.set_listening_active(True)
            if audio:
                try:
                    audio.start_recording()
                except RuntimeError as e:
                    self._log(f"Recording error: {e}")
                    return

        elif state == ClientState.SPEAKING:
            self._status.set_recording("播放中", STATUS_COLOR_SPEAKING)

        elif state == ClientState.RECEIVING:
            self._status.set_recording("接收中", STATUS_COLOR_LISTENING)

        elif state == ClientState.ERROR:
            self._status.set_connection("错误", STATUS_COLOR_ERROR)
            self._controls.set_connected(False)
            self._controls.set_listening_active(False)
            self._auto_recording = False
            self._session_active = False
            if audio:
                audio.stop_recording()
                audio.stop_playback()

        elif state == ClientState.DISCONNECTED:
            self._status.set_connection("未连接", STATUS_COLOR_IDLE)
            self._status.set_recording("停止", STATUS_COLOR_IDLE)
            self._controls.set_connected(False)
            self._controls.set_listening_active(False)
            self._auto_recording = False
            self._session_active = False

    def _on_server_message(self, msg: ServerMessage) -> None:
        self.after(0, self._display_server_message, msg)

    def _display_server_message(self, msg: ServerMessage) -> None:
        t = msg.type
        payload = msg.payload

        if t == "stt":
            text = payload.get("text", "")
            self._log(f"[STT] {text}", tag="recv")

        elif t == "agent":
            text = payload.get("text", "")
            self._log(f"[Agent] {text}", tag="recv")

        elif t == "session_start":
            sid = payload.get("session_id", "")
            reason = payload.get("interrupt_reason", "")
            self._log(f"[Session] started id={sid} reason={reason}", tag="recv")

        elif t == "session_end":
            sid = payload.get("session_id", "")
            self._log(f"[Session] ended id={sid}", tag="recv")
            if self._audio:
                self._audio.stop_playback()

        elif t == "listening_stopped":
            self._log(f"[Listening] stopped", tag="recv")
            self._auto_recording = False
            self._session_active = False
            if self._audio:
                self._audio.stop_recording()
            self._status.set_recording("停止", STATUS_COLOR_IDLE)
            self._controls.set_listening_active(False)

        elif t == "ducking":
            vol = payload.get("volume", 1.0)
            self._log(f"[Ducking] volume={vol}", tag="recv")

        elif t == "error":
            code = payload.get("code", "?")
            detail = payload.get("message", "")
            self._log(f"[Error] {code}: {detail}", tag="error")

        elif t == "close":
            reason = payload.get("reason", "")
            self._log(f"[Close] {reason}", tag="error")

        elif t == "eot_debug":
            self._log(f"[EOT Debug] {payload}", tag="recv")

    def _on_binary_frame(self, data: bytes) -> None:
        if not data:
            if self._audio:
                self._audio.stop_playback()
            return
        if self._audio:
            self._audio.enqueue_audio(data)

    def _on_ws_log(self, msg: str) -> None:
        self.after(0, self._log, msg)

    def _log(self, msg: str, tag: str = "info") -> None:
        self._log_panel.append(msg, tag)

    def _on_close(self) -> None:
        if self._audio:
            self._audio.close()
        if self._ws:
            self._ws.disconnect()
        self.destroy()


def main() -> None:
    app = GuiClient()
    app.mainloop()
