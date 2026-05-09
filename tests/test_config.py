"""Tests for unified GET /api/config."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import hub.config as hub_config
from hub.config import AppConfig, Esp32Config, resolve_eidolon_livekit_client_url
from hub.main import create_app


@pytest.fixture
def client():
    cfg = AppConfig()
    cfg.esp32.livekit_url = "wss://example.test"
    with patch("hub.api.routers.system.config.load_config", return_value=cfg):
        app = create_app(cfg)
        yield TestClient(app)


def test_config_esp32_default_client_type(client: TestClient):
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-1", "fake-jwt"),
    ):
        r = client.get("/api/config", headers={"X-Device-ID": "dev-1"})
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["config"]["identity"] == "dev-1"
    assert data["config"]["token"] == "fake-jwt"
    assert data["config"]["server_url"] == "wss://example.test"
    assert "room_name" in data["config"]


def test_config_esp32_explicit_client_type(client: TestClient):
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-2", "jwt-2"),
    ):
        r = client.get(
            "/api/config",
            params={"client_type": "esp32", "room_name": "r1"},
            headers={"X-Device-ID": "dev-2"},
        )
    assert r.status_code == 200
    assert r.json()["config"]["room_name"] == "r1"


def test_config_esp32_missing_header(client: TestClient):
    r = client.get("/api/config")
    assert r.status_code == 422


def test_config_web(client: TestClient):
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("user-a", "web-jwt"),
    ):
        r = client.get(
            "/api/config",
            params={
                "client_type": "web",
                "room_name": "room-x",
                "participant_name": "user-a",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["identity"] == "user-a"
    assert data["accessToken"] == "web-jwt"


def test_config_web_missing_params(client: TestClient):
    r = client.get("/api/config", params={"client_type": "web", "room_name": "r"})
    assert r.status_code == 422


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
