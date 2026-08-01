"""Replaceable production clock and identifier adapters."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SecureIdGenerator:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(18)}"
