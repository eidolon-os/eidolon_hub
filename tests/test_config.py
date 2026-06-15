"""Tests for unified GET /api/config."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eidolon_sdk.admin import AdminPrecondition
from eidolon_sdk.devices import body_sha256_hex, canonical_request, public_key_fingerprint
from fastapi.testclient import TestClient

import hub.config as hub_config
from hub.config import (
    AppConfig,
    Esp32Config,
    _livekit_from_yaml_and_env,
    resolve_eidolon_livekit_client_url,
)
from hub.core.device_manager import DeviceManager
from hub.main import create_app


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig()
    cfg.esp32.livekit_url = "wss://example.test"
    with patch("hub.api.routers.system.config.load_config", return_value=cfg):
        app = create_app(cfg)
        # Bypass lifespan (which boots LiveKitAdminRuntime + mDNS) by
        # stashing config directly on app.state. Tests that need the
        # admin-lookup path override app.state.admin_client too — see
        # test_config_web_user_id.py.
        app.state.config = cfg
        dm = DeviceManager(tmp_path / "devices.json")
        asyncio.run(dm.load())
        app.state.device_manager = dm
        app.state.admin_client = AsyncMock()
        yield TestClient(app)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _signed_device_headers(
    *,
    device_id: str,
    path_query: str = "/api/config",
    nonce: str = "nonce-1",
    key=None,
    include_public_key: bool = True,
) -> dict[str, str]:
    key = key or ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    timestamp = "0"
    signed = canonical_request(
        method="GET",
        path_query=path_query,
        device_id=device_id,
        nonce=nonce,
        timestamp=timestamp,
        body_hash=body_sha256_hex(),
    )
    headers = {
        "X-Device-ID": device_id,
        "X-Device-Nonce": nonce,
        "X-Device-Timestamp": timestamp,
        "X-Device-Signature": _b64url(key.sign(signed, ec.ECDSA(hashes.SHA256()))),
    }
    if include_public_key:
        headers["X-Device-Public-Key"] = _b64url(public_der)
    return headers


def test_config_esp32_default_client_type(client: TestClient):
    key = ec.generate_private_key(ec.SECP256R1())
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-1", "fake-jwt"),
    ):
        r = client.get(
            "/api/config",
            headers=_signed_device_headers(device_id="dev-1", key=key),
        )
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["status"] == "pending_approval"
    assert data["config"]["identity"] == "dev-1"
    assert data["config"]["token"] == "fake-jwt"
    assert data["config"]["server_url"] == "wss://example.test"
    assert data["config"]["room_name"] == "eidolon-pending"
    assert data["device"]["approved"] is False
    assert data["device"]["bound"] is False
    public_der = key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    assert data["device"]["fingerprint"] == public_key_fingerprint(_b64url(public_der))


def test_config_esp32_explicit_client_type(client: TestClient):
    dm = client.app.state.device_manager
    key = ec.generate_private_key(ec.SECP256R1())
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-2", "pending-jwt"),
    ):
        r0 = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers=_signed_device_headers(
                device_id="dev-2",
                path_query="/api/config?client_type=esp32",
                key=key,
            ),
        )
    assert r0.status_code == 200
    asyncio.run(dm.approve("dev-2"))
    client.app.state.admin_client.resolve_device.return_value = {
        "context": {"device_id": "dev-2", "user_id": "u", "agent_id": "a"}
    }
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-2", "jwt-2"),
    ):
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32"), ("room_name", "r1")],
            headers=_signed_device_headers(
                device_id="dev-2",
                path_query="/api/config?client_type=esp32&room_name=r1",
                nonce="nonce-2",
                key=key,
                include_public_key=False,
            ),
        )
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert r.json()["config"]["room_name"] == "r1"
    assert r.json()["device"]["approved"] is True
    assert r.json()["device"]["bound"] is True


def test_config_esp32_approved_waits_for_binding(client: TestClient):
    dm = client.app.state.device_manager
    key = ec.generate_private_key(ec.SECP256R1())
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-3", "pending-jwt"),
    ):
        r0 = client.get(
            "/api/config",
            headers=_signed_device_headers(device_id="dev-3", key=key),
        )
    assert r0.status_code == 200
    asyncio.run(dm.approve("dev-3"))
    client.app.state.admin_client.resolve_device.side_effect = AdminPrecondition(
        412, "device is not bound"
    )
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-3", "jwt-3"),
    ):
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers=_signed_device_headers(
                device_id="dev-3",
                path_query="/api/config?client_type=esp32",
                nonce="nonce-2",
                key=key,
                include_public_key=False,
            ),
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "waiting_binding"
    assert data["config"]["room_name"] == "eidolon-pending"
    assert data["device"]["approved"] is True
    assert data["device"]["bound"] is False


def test_config_esp32_missing_header(client: TestClient):
    r = client.get("/api/config")
    assert r.status_code == 422


def test_config_esp32_requires_device_signature(client: TestClient):
    r = client.get("/api/config", headers={"X-Device-ID": "dev-auth"})
    assert r.status_code == 422
    assert "X-Device-Signature" in r.json()["detail"]


def test_config_esp32_rejects_replayed_nonce(client: TestClient):
    key = ec.generate_private_key(ec.SECP256R1())
    headers = _signed_device_headers(device_id="dev-replay", nonce="same", key=key)
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-replay", "jwt"),
    ):
        r1 = client.get("/api/config", headers=headers)
        r2 = client.get("/api/config", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 409


def test_config_esp32_rejects_public_key_change(client: TestClient):
    key1 = ec.generate_private_key(ec.SECP256R1())
    key2 = ec.generate_private_key(ec.SECP256R1())
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-key", "jwt"),
    ):
        r1 = client.get(
            "/api/config",
            headers=_signed_device_headers(device_id="dev-key", nonce="n1", key=key1),
        )
        r2 = client.get(
            "/api/config",
            headers=_signed_device_headers(device_id="dev-key", nonce="n2", key=key2),
        )
    assert r1.status_code == 200
    assert r2.status_code == 401


def test_config_web_missing_room(client: TestClient):
    """Web path requires both ``room_name`` and ``user_id``."""
    r = client.get(
        "/api/config",
        params={"client_type": "web", "user_id": "manson"},
    )
    assert r.status_code == 422


def test_config_web_missing_user_id(client: TestClient):
    """Phase 33.A6: ``user_id`` is unconditionally required (rollback
    bypass removed). Happy path lives in test_config_web_user_id.py
    where admin_client is mocked."""
    r = client.get(
        "/api/config",
        params={"client_type": "web", "room_name": "r"},
    )
    assert r.status_code == 422
    assert "user_id" in r.json()["detail"]


def test_livekit_rejects_inline_secrets_in_yaml():
    with pytest.raises(ValueError, match="placeholder"):
        _livekit_from_yaml_and_env({"livekit": {"api_key": "leaked"}})


def test_livekit_accepts_env_name_placeholders(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    cfg = _livekit_from_yaml_and_env(
        {
            "livekit": {
                "api_key": "LIVEKIT_API_KEY",
                "api_secret": "LIVEKIT_API_SECRET",
            }
        }
    )
    assert cfg.api_key == "k"
    assert cfg.api_secret == "s"


def test_resolve_full_url_override():
    esp32 = Esp32Config(livekit_url="wss://lk.example.com/livekit")
    assert (
        resolve_eidolon_livekit_client_url(
            esp32,
            request_host="ignored",
            request_scheme="http",
        )
        == "wss://lk.example.com/livekit"
    )


def test_resolve_explicit_ip():
    esp32 = Esp32Config(
        livekit_url="",
        livekit_ip="192.168.3.204",
        livekit_port=7880,
        livekit_scheme="",
    )
    assert (
        resolve_eidolon_livekit_client_url(
            esp32,
            request_host="10.0.0.1",
            request_scheme="https",
        )
        == "ws://192.168.3.204:7880"
    )


def test_resolve_explicit_ip_wss():
    esp32 = Esp32Config(
        livekit_url="",
        livekit_ip="192.168.3.204",
        livekit_port=7880,
        livekit_scheme="wss",
    )
    assert (
        resolve_eidolon_livekit_client_url(
            esp32,
            request_host="10.0.0.1",
            request_scheme="http",
        )
        == "wss://192.168.3.204:7880"
    )


def test_resolve_auto_uses_request_host():
    esp32 = Esp32Config(livekit_url="", livekit_ip="", livekit_port=7880, livekit_scheme="")
    assert (
        resolve_eidolon_livekit_client_url(
            esp32,
            request_host="eidolon-hub.local",
            request_scheme="http",
        )
        == "ws://eidolon-hub.local:7880"
    )


def test_resolve_auto_https_hub():
    esp32 = Esp32Config(livekit_url="", livekit_ip="auto", livekit_port=7880, livekit_scheme="")
    assert (
        resolve_eidolon_livekit_client_url(
            esp32,
            request_host="hub.example.com",
            request_scheme="https",
        )
        == "wss://hub.example.com:7880"
    )


def test_resolve_auto_empty_host_uses_lan(monkeypatch):
    esp32 = Esp32Config(livekit_url="", livekit_ip="", livekit_port=7880, livekit_scheme="")
    monkeypatch.setattr(hub_config, "_outbound_ipv4", lambda: "192.168.55.2")
    url = resolve_eidolon_livekit_client_url(esp32, request_host="", request_scheme="http")
    assert url == "ws://192.168.55.2:7880"


def test_resolve_auto_localhost_uses_lan(monkeypatch):
    esp32 = Esp32Config(livekit_url="", livekit_ip="auto", livekit_port=7880, livekit_scheme="")
    monkeypatch.setattr(hub_config, "_outbound_ipv4", lambda: "10.0.0.7")
    url = resolve_eidolon_livekit_client_url(esp32, request_host="127.0.0.1", request_scheme="http")
    assert url == "ws://10.0.0.7:7880"


def test_resolve_auto_lan_probe_fails_raises(monkeypatch):
    esp32 = Esp32Config(livekit_url="", livekit_ip="", livekit_port=7880, livekit_scheme="")
    monkeypatch.setattr(hub_config, "_outbound_ipv4", lambda: "127.0.0.1")
    with pytest.raises(ValueError):
        resolve_eidolon_livekit_client_url(esp32, request_host="localhost", request_scheme="http")
