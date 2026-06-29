"""GET /api/config?client_type=web validates owner_id against admin and
mints a LiveKit token. Hub does NOT sign any runtime JWT — that lives in
channel.

Three layers under test:

  - **Validation**: owner_id required for web; deprecated user_id works
    only as a compatibility alias.
  - **Resolution**: admin lookup happens; 404 if owner not in admin, 503
    if admin unreachable, 502 if admin 5xx.
  - **Token shape**: LiveKit identity = owner_id (channel reads this).
    participant.metadata carries owner_id + display_name hints only —
    NEVER a runtime/device token.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from eidolon_sdk.biz.admin import (
    AdminNotFound,
    AdminUnreachable,
    AdminUpstreamError,
)
from fastapi.testclient import TestClient

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
    from eidolon_sdk.biz.admin import AdminClient

    return AsyncMock(spec=AdminClient)


@pytest.fixture
def client(cfg: AppConfig, fake_admin_client):
    with patch("hub.api.routers.system.config.load_config", return_value=cfg):
        app = create_app(cfg)
        app.state.config = cfg
        app.state.admin_client = fake_admin_client
        yield TestClient(app)


def _owner_view(owner_id: str, *, display_name: str | None = None) -> dict[str, Any]:
    """Minimal OwnerView shape — only fields hub reads."""
    return {
        "owner_id": owner_id,
        "display_name": display_name or owner_id,
        "kind": "human",
        "status": "active",
    }


# ---- validation ---------------------------------------------------------


def test_web_requires_owner_id_when_enabled(client: TestClient):
    """No owner_id -> 422 with actionable message."""
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r"},
    )
    assert r.status_code == 422
    assert "owner_id" in r.json()["detail"]


# ---- resolution failures ------------------------------------------------


def test_web_owner_not_in_admin_returns_404(client: TestClient, fake_admin_client):
    """admin GET /api/owners/X -> 404 -> hub propagates as 404."""
    fake_admin_client.get_owner.side_effect = AdminNotFound(
        "owner 'ghost' not found in admin registry"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r", "owner_id": "ghost"},
    )
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_web_admin_unreachable_returns_503(client: TestClient, fake_admin_client):
    fake_admin_client.get_owner.side_effect = AdminUnreachable(
        "admin GET /api/owners failed: ConnectError"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r", "owner_id": "manson"},
    )
    assert r.status_code == 503


def test_web_admin_upstream_5xx_returns_502(client: TestClient, fake_admin_client):
    fake_admin_client.get_owner.side_effect = AdminUpstreamError(
        500, "memory subprocess died"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r", "owner_id": "manson"},
    )
    assert r.status_code == 502


# ---- happy path ---------------------------------------------------------


def test_web_happy_path_lk_identity_is_owner_id(client: TestClient, fake_admin_client):
    """LK token identity == owner_id so channel can use it as the resolve key."""
    fake_admin_client.get_owner.return_value = _owner_view(
        "manson", display_name="Manson Li"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "owner_id": "manson"},
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
    assert meta["kind"] == "owner"
    assert meta["display_name"] == "Manson Li"
    assert meta["owner_id"] == "manson"


def test_web_user_id_alias_still_maps_to_owner_id(
    client: TestClient, fake_admin_client
):
    fake_admin_client.get_owner.return_value = _owner_view("manson")
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "user_id": "manson"},
    )
    assert r.status_code == 200, r.text
    fake_admin_client.get_owner.assert_called_once_with("manson")


def test_web_defaults_full_duplex(client: TestClient, fake_admin_client):
    """Phase 4: web clients default to full_duplex (browser tab has no
    half-duplex hardware constraint); the header can still override."""
    fake_admin_client.get_owner.return_value = _owner_view("manson")
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "owner_id": "manson"},
    )
    assert r.status_code == 200, r.text
    lk_payload = jwt.decode(
        r.json()["accessToken"], options={"verify_signature": False}
    )
    meta = json.loads(lk_payload["metadata"])
    assert meta["interaction_mode"] == "full_duplex"

    r2 = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "owner_id": "manson"},
        headers={"X-Device-Interaction-Mode": "half_duplex"},
    )
    assert r2.status_code == 200, r2.text
    meta2 = json.loads(
        jwt.decode(r2.json()["accessToken"], options={"verify_signature": False})[
            "metadata"
        ]
    )
    assert meta2["interaction_mode"] == "half_duplex"


def test_web_metadata_carries_no_device_token(
    client: TestClient, fake_admin_client
):
    """Hub MUST NOT embed runtime/device tokens in participant.metadata."""
    fake_admin_client.get_owner.return_value = _owner_view("manson")
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "owner_id": "manson"},
    )
    assert r.status_code == 200, r.text
    lk_payload = jwt.decode(r.json()["accessToken"], options={"verify_signature": False})
    raw_meta = lk_payload.get("metadata")
    if raw_meta:
        meta = json.loads(raw_meta)
        assert "device_token" not in meta, (
            "regression: hub should not sign or embed device_token — channel "
            "is the runtime identity authority."
        )
        assert "runtime_token" not in meta
        # Hints are OK and useful for debugging.
        assert meta.get("owner_id") == "manson"


def test_web_admin_lookup_is_unconditional(
    client: TestClient, fake_admin_client
):
    """Phase 33.A6: removed the ``runtime_admin.enabled=false``
    rollback path — admin lookup is now mandatory for the web flow.
    Any owner_id MUST round-trip through admin first; channel 32.D
    already has no static-token fallback, so a bypass would only mint
    LK tokens channel rejects on next /api/resolve call."""
    # Hub always reaches out — even for a perfectly valid token request
    # we expect get_owner to be invoked.
    from eidolon_sdk.biz.admin import AdminNotFound
    fake_admin_client.get_owner.side_effect = AdminNotFound("unknown")
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "R", "owner_id": "anyone"},
    )
    # 404 not 200 — the rollback path that would have minted a token
    # for an unverified user is gone.
    assert r.status_code == 404
    fake_admin_client.get_owner.assert_called_once_with("anyone")
