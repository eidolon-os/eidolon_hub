"""GET /api/config?client_type=web&device_id=... mints a device-identity
token for a companion's host-local *web body*.

Unlike the legacy owner-only web path (identity=owner_id), a web body:
  - resolves + binding-checks against admin via ``resolve_device``,
  - mints identity=device_id, kind=device (channel resolves via
    ``/api/resolve/device/{id}``), so the body runs its companion's persona,
  - has NO browser ECDSA (admin trust),
  - derives its voice room from the device when ``room_name`` is omitted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from eidolon_sdk.biz.admin import (
    AdminResolveNotFound,
    AdminResolvePrecondition,
    ResolvedContext,
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
    return c


@pytest.fixture
def client(cfg: AppConfig):
    with patch("hub.api.routers.system.config.load_config", return_value=cfg):
        app = create_app(cfg)
        app.state.config = cfg
        app.state.admin_client = AsyncMock()
        app.state.admin_resolve_client = AsyncMock()
        yield TestClient(app)


def _resolved(
    *,
    device_id: str = "web-c1",
    owner_id: str = "manson",
    companion_id: str = "c1",
    interaction_mode: str | None = None,
) -> ResolvedContext:
    return ResolvedContext(
        owner_id=owner_id,
        companion_id=companion_id,
        device_id=device_id,
        memory_realm_id="realm-1",
        genome_id="genome-1",
        schema_version="eidolon.persona_genome.v1",
        genome_hash="sha256:test",
        compiler_version="eidolon.persona_compiler.v1",
        interaction_mode=interaction_mode,
    )


def test_web_body_mints_device_identity_token(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.return_value = _resolved()
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("web-c1", "fake-jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params={
                "client_type": "web",
                "owner_id": "manson",
                "companion_id": "c1",
                "device_id": "web-c1",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity"] == "web-c1"
    assert body["accessToken"] == "fake-jwt"

    kwargs = gen.call_args.kwargs
    assert kwargs["participant_name"] == "web-c1"  # -> LK identity
    meta = kwargs["participant_metadata"]
    assert meta["kind"] == "device"
    assert meta["device_id"] == "web-c1"
    assert meta["companion_id"] == "c1"
    assert meta["owner_id"] == "manson"
    client.app.state.admin_resolve_client.resolve_device.assert_awaited_once_with("web-c1")


def test_web_body_room_derived_from_device_when_omitted(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.return_value = _resolved()
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("web-c1", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params={"client_type": "web", "owner_id": "manson", "device_id": "web-c1"},
        )
    assert r.status_code == 200, r.text
    assert gen.call_args.kwargs["room_name"].startswith("device-web-c1-")


def test_web_body_honors_explicit_room(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.return_value = _resolved()
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("web-c1", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params={
                "client_type": "web",
                "owner_id": "manson",
                "device_id": "web-c1",
                "room_name": "custom-room",
            },
        )
    assert r.status_code == 200, r.text
    assert gen.call_args.kwargs["room_name"] == "custom-room"


def test_web_body_admin_override_wins_interaction_mode(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.return_value = _resolved(
        interaction_mode="half_duplex"
    )
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("web-c1", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params={"client_type": "web", "owner_id": "manson", "device_id": "web-c1"},
        )
    assert r.status_code == 200, r.text
    assert gen.call_args.kwargs["participant_metadata"]["interaction_mode"] == "half_duplex"


def test_web_body_rejects_owner_mismatch(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.return_value = _resolved(
        owner_id="someone-else"
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "owner_id": "manson", "device_id": "web-c1"},
    )
    assert r.status_code == 403


def test_web_body_rejects_companion_mismatch(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.return_value = _resolved(
        companion_id="other-companion"
    )
    r = client.get(
        "/api/config",
        params={
            "client_type": "web",
            "owner_id": "manson",
            "companion_id": "c1",
            "device_id": "web-c1",
        },
    )
    assert r.status_code == 403


def test_web_body_unbound_device_returns_409(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.side_effect = (
        AdminResolvePrecondition(409, "device 'web-c1' is not bound to a companion")
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "owner_id": "manson", "device_id": "web-c1"},
    )
    assert r.status_code == 409


def test_web_body_unknown_device_returns_404(client: TestClient):
    client.app.state.admin_resolve_client.resolve_device.side_effect = (
        AdminResolveNotFound("device 'ghost' not found")
    )
    r = client.get(
        "/api/config",
        params={"client_type": "web", "owner_id": "manson", "device_id": "ghost"},
    )
    assert r.status_code == 404


def test_legacy_owner_path_needs_room_and_skips_resolve(client: TestClient):
    """No device_id -> legacy owner path: room_name still required, and the
    web-body resolve is never invoked."""
    r = client.get(
        "/api/config",
        params={"client_type": "web", "owner_id": "manson"},
    )
    assert r.status_code == 422
    client.app.state.admin_resolve_client.resolve_device.assert_not_called()
