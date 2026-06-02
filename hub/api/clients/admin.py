"""Thin HTTP client over the admin gateway.

Phase 32.A: hub's ``/api/config`` web path needs to confirm the
``user_id`` exists in admin (Phase 29.H — 杜绝陌生 user_id 入口) and
fetch the display_name to use as LiveKit participant name. The deeper
lookup (active_agent, template_id) lives in channel under plan D —
channel signs the runtime device_token, hub just validates and mints
the LiveKit token.

We deliberately stay BELOW the abstraction admin offers: just GET
/api/users/{id}. ``get_agent`` was here in an earlier 32.A iteration
that signed device_tokens at hub; the cleaner plan D moved that to
channel, so this client shrinks accordingly.

Error shape mirrors admin's own sub-project client pattern: separate
sentinels for 404 (NotFound), connection failure (Unreachable), and
other upstream codes (UpstreamError). Callers translate to
HTTPException.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx


class AdminClientError(Exception):
    """Base — never raised directly, only subclasses."""


class AdminNotFound(AdminClientError):
    """admin returned 404 (user id doesn't exist)."""


class AdminUpstreamError(AdminClientError):
    """admin returned a non-2xx response other than 404."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"admin upstream {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class AdminUnreachable(AdminClientError):
    """Connection / DNS / timeout — admin process likely down."""


def _unwrap_detail(body: str) -> str:
    """admin/fastapi wraps errors as ``{"detail": "..."}``; strip the
    envelope so the message we log stays single-layer."""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)
    return body


class AdminClient:
    """Read-only wrapper. The FastAPI lifespan owns the underlying
    httpx client and reuses it across requests."""

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """``GET /api/users/{user_id}`` — returns the UserView envelope.

        hub reads ``spec.user_id`` (echo back) and ``spec.display_name``
        for the LK participant name. tenant_id / active_agent_id are NOT
        looked up here — channel does that resolution under plan D.

        Raises ``AdminNotFound`` for 404, ``AdminUnreachable`` for
        connection failures, ``AdminUpstreamError`` otherwise.
        """
        url = f"{self._base}/api/users/{quote(user_id, safe='')}"
        try:
            r = await self._http.get(url, timeout=5.0)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise AdminUnreachable(f"admin GET /api/users failed: {exc}") from exc

        if r.status_code == 404:
            raise AdminNotFound(_unwrap_detail(r.text))
        if r.status_code >= 400:
            raise AdminUpstreamError(r.status_code, _unwrap_detail(r.text))
        return r.json()
