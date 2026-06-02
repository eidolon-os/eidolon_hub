"""Phase 32.A (plan D): GET /api/config?client_type=web validates
user_id against admin and mints a LiveKit token. Hub does NOT sign any
device JWT — that lives in channel under plan D.

Three layers under test:

  - **Validation**: user_id required when runtime_admin.enabled=true; 422.
  - **Resolution**: admin lookup happens; 404 if user not in admin, 503
    if admin unreachable, 502 if admin 5xx.
  - **Token shape**: LiveKit identity = user_id (channel reads this).
    participant.metadata carries user_id + display_name hints only —
    NEVER a device token.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from hub.api.clients import (
    AdminNotFound,
    AdminUnreachable,
    AdminUpstreamError,
)
from hub.config import AppConfig
from hub.main import create_app


@pytest.fixture
def cfg() -> AppConfig:
    c = AppConfig()
    c.esp32.livekit_url = "wss://example.test"
    c.livekit.api_key = "test-key"
    c.livekit.api_secret = "test-secret"
    c.runtime_admin.enabled = True
    return c


@pytest.fixture
def fake_admin_client():
    from hub.api.clients import AdminClient

    return AsyncMock(spec=AdminClient)


@pytest.fixture
def client(cfg: AppConfig, fake_admin_client):
    with patch("hub.api.routers.system.config.load_config", return_value=cfg):
        app = create_app(cfg)
        app.state.config = cfg
        app.state.admin_client = fake_admin_client
        yield TestClient(app)


def _user_view(user_id: str, *, display_name: str | None = None) -> dict[str, Any]:
    """Minimal UserView shape — only fields hub reads under plan D."""
    return {
        "spec": {
            "user_id": user_id,
            "tenant_id": "default",
            "display_name": display_name or user_id,
        },
        "health": {"worker_running": True, "mcp_reachable": True, "palace_initialized": True},
        "active_agent_id": None,  # not used by hub under plan D
        "agent_ids": [],
    }


# ---- validation ---------------------------------------------------------


def test_web_requires_user_id_when_enabled(client: TestClient):
    """No user_id → 422 with actionable message (Phase 32.A 严格)."""
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r"},
    )
    assert r.status_code == 422
    assert "user_id" in r.json()["detail"]


# ---- resolution failures ------------------------------------------------


def test_web_user_not_in_admin_returns_404(client: TestClient, fake_admin_client):
    """admin GET /api/users/X → 404 → hub propagates as 404 (no silent
    fallback to a default user)."""
    fake_admin_client.get_user.side_effect = AdminNotFound(
        "user 'ghost' not found in admin registry"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r", "user_id": "ghost"},
    )
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_web_admin_unreachable_returns_503(client: TestClient, fake_admin_client):
    fake_admin_client.get_user.side_effect = AdminUnreachable(
        "admin GET /api/users failed: ConnectError"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r", "user_id": "manson"},
    )
    assert r.status_code == 503


def test_web_admin_upstream_5xx_returns_502(client: TestClient, fake_admin_client):
    fake_admin_client.get_user.side_effect = AdminUpstreamError(
        500, "memory subprocess died"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r", "user_id": "manson"},
    )
    assert r.status_code == 502


# ---- happy path ---------------------------------------------------------


def test_web_happy_path_lk_identity_is_user_id(client: TestClient, fake_admin_client):
    """Plan D contract: LK token identity == user_id so channel can use
    it as the admin lookup key. Display name from admin's record shows
    up as LK participant ``name`` (cosmetic — channel ignores it)."""
    fake_admin_client.get_user.return_value = _user_view(
        "manson", display_name="Manson Li"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "user_id": "manson"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity"] == "manson"

    lk_payload = jwt.decode(body["accessToken"], options={"verify_signature": False})
    assert lk_payload["sub"] == "manson"  # LK puts identity in sub
    # display_name lives in participant.metadata (the LK display name
    # field gets the identity for compat with legacy clients that read
    # ``participant.name``). Tests for that field are in the metadata
    # check above; here we just pin "identity round-trips correctly".
    raw_meta = lk_payload.get("metadata")
    assert raw_meta, "expected hint metadata for debugging"
    meta = json.loads(raw_meta)
    assert meta["display_name"] == "Manson Li"
    assert meta["user_id"] == "manson"


def test_web_metadata_carries_no_device_token(
    client: TestClient, fake_admin_client
):
    """**Negative test** pinning plan D's security stance: hub MUST NOT
    embed a device_token in participant.metadata. If a future refactor
    regresses to plan A, this test fails loudly."""
    fake_admin_client.get_user.return_value = _user_view("manson")
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "user_id": "manson"},
    )
    assert r.status_code == 200, r.text
    lk_payload = jwt.decode(r.json()["accessToken"], options={"verify_signature": False})
    raw_meta = lk_payload.get("metadata")
    if raw_meta:
        meta = json.loads(raw_meta)
        assert "device_token" not in meta, (
            "regression: hub should not sign or embed device_token under "
            "plan D — channel is the authority. Move signing back to "
            "channel/agent if this is intentional."
        )
        # Hints are OK and useful for debugging.
        assert meta.get("user_id") == "manson"


def test_web_admin_lookup_is_unconditional(
    client: TestClient, fake_admin_client
):
    """Phase 33.A6: removed the ``runtime_admin.enabled=false``
    rollback path — admin lookup is now mandatory for the web flow.
    Any user_id MUST round-trip through admin first; channel 32.D
    already has no static-token fallback, so a bypass would only mint
    LK tokens channel rejects on next /api/resolve call."""
    # Hub always reaches out — even for a perfectly valid token request
    # we expect get_user to be invoked.
    from hub.api.clients import AdminNotFound
    fake_admin_client.get_user.side_effect = AdminNotFound("unknown")
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "user_id": "anyone"},
    )
    # 404 not 200 — the rollback path that would have minted a token
    # for an unverified user is gone.
    assert r.status_code == 404
    fake_admin_client.get_user.assert_called_once_with("anyone")
