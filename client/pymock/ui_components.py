"""Custom Tkinter widgets for the GUI client."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .constants import STATUS_COLOR_IDLE, STATUS_COLOR_CONNECTED, STATUS_COLOR_ERROR


class StatusBar(ttk.Frame):
    """Top status bar showing connection, recording, and format status."""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, relief=tk.SUNKEN, padding=(8, 4))
        self._labels: dict[str, ttk.Label] = {}
        self._indicator_labels: dict[str, tk.Label] = {}

        row = 0
        items = [
            ("conn", "连接状态"),
            ("rec", "录音状态"),
            ("fmt", "音频格式"),
        ]
        for key, text in items:
            ttk.Label(self, text=f"{text}:").grid(row=row, column=0, sticky="w", padx=(0, 4))
            indicator = tk.Label(self, text="--", width=10, anchor="w")
            indicator.grid(row=row, column=1, sticky="w", padx=(0, 16))
            self._indicator_labels[key] = indicator
            row += 1

        self.columnconfigure(1, weight=1)

    def set(self, key: str, value: str, color: str | None = None) -> None:
        """Update a status indicator."""
        lbl = self._indicator_labels.get(key)
        if lbl:
            lbl.config(text=value, fg=color or "black")

    def set_connection(self, status: str, color: str | None = None) -> None:
        self.set("conn", status, color)

    def set_recording(self, status: str, color: str | None = None) -> None:
        self.set("rec", status, color)

    def set_format(self, fmt: str) -> None:
        self.set("fmt", fmt)


class LogPanel(ttk.Frame):
    """Scrollable text widget showing logs and messages."""

    def __init__(self, parent: tk.Widget, height: int = 15) -> None:
        super().__init__(parent)
        scrollbar = ttk.Scrollbar(self)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._text = tk.Text(
            self,
            height=height,
            wrap=tk.WORD,
            yscrollcommand=scrollbar.set,
            font=("Courier New", 13),
            state=tk.DISABLED,
        )
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self._text.yview)

        self._text.tag_config("info", foreground="#888888")
        self._text.tag_config("recv", foreground="#5bc0de")
        self._text.tag_config("send", foreground="#5cb85c")
        self._text.tag_config("error", foreground="#d9534f")
        self._text.tag_config("audio", foreground="#9b59b6")
        self._text.tag_config("ducking", foreground="#f0ad4e")
        self._text.tag_config("session", foreground="#9b59b6")

    def append(self, message: str, tag: str = "info") -> None:
        """Append a line to the log panel."""
        self._text.config(state=tk.NORMAL)
        self._text.insert(tk.END, message + "\n", tag)
        self._text.see(tk.END)
        self._text.config(state=tk.DISABLED)

    def clear(self) -> None:
        """Clear the log panel."""
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.config(state=tk.DISABLED)


class ControlPanel(ttk.Frame):
    """Button row for the four main actions."""

    def __init__(
        self,
        parent: tk.Widget,
        on_connect: callable,
        on_start_listening: callable,
        on_stop_listening: callable,
        on_auto_chat: callable,
    ) -> None:
        super().__init__(parent, padding=(8, 8))
        self._on_connect = on_connect
        self._on_start_listening = on_start_listening
        self._on_stop_listening = on_stop_listening
        self._on_auto_chat = on_auto_chat

        style = ttk.Style()
        style.configure("Action.TButton", font=("Helvetica", 10, "bold"), padding=6)

        self._btn_connect = ttk.Button(
            self, text="连接", style="Action.TButton", command=self._emit_connect
        )
        self._btn_start = ttk.Button(
            self, text="开始监听", style="Action.TButton", command=self._emit_start, state=tk.DISABLED
        )
        self._btn_stop = ttk.Button(
            self, text="结束监听", style="Action.TButton", command=self._emit_stop, state=tk.DISABLED
        )
        self._btn_auto = ttk.Button(
            self, text="自动对话", style="Action.TButton", command=self._emit_auto, state=tk.DISABLED
        )

        self._btn_connect.pack(side=tk.LEFT, padx=4)
        self._btn_start.pack(side=tk.LEFT, padx=4)
        self._btn_stop.pack(side=tk.LEFT, padx=4)
        self._btn_auto.pack(side=tk.LEFT, padx=4)

    def set_connected(self, connected: bool) -> None:
        """Enable/disable connect button text (connected vs connect) and start/auto buttons."""
        self._btn_connect.config(text="断开" if connected else "连接")
        state = tk.NORMAL if connected else tk.DISABLED
        self._btn_start.config(state=state)
        self._btn_auto.config(state=state)

    def set_listening_active(self, active: bool) -> None:
        """Show stop button during active listening; hide it when stopped."""
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL if active else tk.DISABLED)
        self._btn_auto.config(state=tk.DISABLED)

    def set_recording(self, recording: bool) -> None:
        """Deprecated: use set_listening_active instead."""
        self._btn_start.config(state=tk.DISABLED if recording else tk.NORMAL)
        self._btn_stop.config(state=tk.NORMAL if recording else tk.DISABLED)
        self._btn_auto.config(state=tk.DISABLED)

    def _emit_connect(self) -> None:
        self._on_connect()

    def _emit_start(self) -> None:
        self._on_start_listening()

    def _emit_stop(self) -> None:
        self._on_stop_listening()

    def _emit_auto(self) -> None:
        self._on_auto_chat()
