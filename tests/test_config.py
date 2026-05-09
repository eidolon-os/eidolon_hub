"""Tests for unified GET /api/config."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from hub.config import AppConfig
from hub.main import create_app


@pytest.fixture
def client():
    cfg = AppConfig()
    cfg.esp32.server_url = "wss://example.test"
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
