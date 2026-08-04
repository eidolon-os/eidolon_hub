"""Replaceable production clock and identifier adapters."""

from __future__ import annotations

import fcntl
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SecureIdGenerator:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(18)}"


class LocalProcessLock:
    """Prevent multiple local Hub processes from sharing one SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("another Eidolon Hub process owns the local database") from exc
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
