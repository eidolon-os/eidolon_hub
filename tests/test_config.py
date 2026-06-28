"""Tests for unified GET /api/config."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eidolon_data import DataSettings, DataStore
from eidolon_data.adapters import EidolonDataDeviceRegistryRepository
from eidolon_sdk.biz.admin import AdminPrecondition
from eidolon_sdk.biz.devices import body_sha256_hex, canonical_request, public_key_fingerprint
from fastapi.testclient import TestClient

import hub.config as hub_config
from hub.config import (
    AppConfig,
    Esp32Config,
    _device_session_from_yaml,
    _livekit_from_yaml_and_env,
    resolve_eidolon_livekit_client_url,
)
from hub.core.device_manager import DeviceManager
from hub.main import create_app


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig()
    cfg.esp32.livekit_url = "wss://example.test"
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    with patch("hub.api.routers.system.config.load_config", return_value=cfg):
        app = create_app(cfg)
        # Bypass lifespan (which boots LiveKitAdminRuntime + mDNS) by
        # stashing config directly on app.state. Tests that need the
        # admin-lookup path override app.state.admin_client too — see
        # test_config_web_user_id.py.
        app.state.config = cfg
        dm = DeviceManager(EidolonDataDeviceRegistryRepository(store, owner_id="owner-test"))
        asyncio.run(dm.load())
        app.state.device_manager = dm
        app.state.admin_client = AsyncMock()
        yield TestClient(app)
    asyncio.run(store.close())


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
    stored = client.app.state.device_manager.get("dev-1")
    assert stored is not None
    assert stored.metadata["last_ip"] == "testclient"
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


def _approve_and_resolve(client: TestClient, device_id: str, key) -> None:
    """Drive a device to ACTIVE: first config (pending) → approve → admin
    resolve_device returns a context."""
    dm = client.app.state.device_manager
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=(device_id, "pending-jwt"),
    ):
        r0 = client.get(
            "/api/config",
            headers=_signed_device_headers(device_id=device_id, key=key),
        )
    assert r0.status_code == 200
    asyncio.run(dm.approve(device_id))
    client.app.state.admin_client.resolve_device.return_value = {
        "context": {"device_id": device_id, "user_id": "u", "agent_id": "a"}
    }


def test_config_esp32_stamps_interaction_mode_from_header(client: TestClient):
    """Phase 4: the device-declared mode lands in the voice-room token's
    participant_metadata so channel can pick a per-session turn policy."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-mode", key)
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-mode", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-mode",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-mode",
                    key=key,
                    include_public_key=False,
                ),
                "X-Device-Interaction-Mode": "full_duplex",
            },
        )
    assert r.status_code == 200
    # First generate_token call mints the voice-room token.
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["kind"] == "device"
    assert voice_meta["interaction_mode"] == "full_duplex"


def test_config_esp32_defaults_half_duplex_when_header_absent(client: TestClient):
    """Defense default (plan §1): a device that doesn't declare a mode is
    treated as half_duplex — no accidental barge-in."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-default", key)
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-default", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers=_signed_device_headers(
                device_id="dev-default",
                path_query="/api/config?client_type=esp32",
                nonce="nonce-default",
                key=key,
                include_public_key=False,
            ),
        )
    assert r.status_code == 200
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["interaction_mode"] == "half_duplex"


def test_config_esp32_invalid_mode_degrades_to_half(client: TestClient):
    """An unrecognized header value degrades to the safe device default."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-bad", key)
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-bad", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-bad",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-bad",
                    key=key,
                    include_public_key=False,
                ),
                "X-Device-Interaction-Mode": "duplexish",
            },
        )
    assert r.status_code == 200
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["interaction_mode"] == "half_duplex"


def test_config_esp32_stamps_proactive_session_intent_from_header(client: TestClient):
    """Phase 3 (B2): a proactive wake's X-Device-Session-Intent lands in the
    voice-token metadata so channel suppresses the welcome. Value must be the
    canonical channel literal (proactive_initiated), not a bare 'proactive'."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-intent", key)
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-intent", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-intent",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-intent",
                    key=key,
                    include_public_key=False,
                ),
                "X-Device-Session-Intent": "proactive_initiated",
            },
        )
    assert r.status_code == 200
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["session_intent"] == "proactive_initiated"


def test_config_esp32_session_intent_defaults_user_initiated(client: TestClient):
    """Absent / unrecognized intent header degrades to user_initiated (welcome
    plays) — a bad value can never spoof a proactive session."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-intent2", key)
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-intent2", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-intent2",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-intent2",
                    key=key,
                    include_public_key=False,
                ),
                "X-Device-Session-Intent": "proactive",  # not the canonical literal
            },
        )
    assert r.status_code == 200
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["session_intent"] == "user_initiated"


def test_config_esp32_admin_override_beats_device_header(client: TestClient):
    """Phase 6: an admin per-device interaction_mode (on the binding) wins over
    the device's self-declared header."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-override", key)
    client.app.state.admin_client.resolve_device.return_value = {
        "context": {
            "device_id": "dev-override",
            "user_id": "u",
            "agent_id": "a",
            "interaction_mode": "full_duplex",
        }
    }
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-override", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-override",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-override",
                    key=key,
                    include_public_key=False,
                ),
                # Device says half; admin override says full → admin wins.
                "X-Device-Interaction-Mode": "half_duplex",
            },
        )
    assert r.status_code == 200
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["interaction_mode"] == "full_duplex"


def test_config_esp32_no_admin_override_keeps_device_header(client: TestClient):
    """Without an admin override, the device-declared header value is used."""
    key = ec.generate_private_key(ec.SECP256R1())
    _approve_and_resolve(client, "dev-noov", key)
    # _approve_and_resolve sets a context WITHOUT interaction_mode.
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-noov", "jwt"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-noov",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-noov",
                    key=key,
                    include_public_key=False,
                ),
                "X-Device-Interaction-Mode": "full_duplex",
            },
        )
    assert r.status_code == 200
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["interaction_mode"] == "full_duplex"


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


def test_config_esp32_dev_direct_voice_allows_approved_unbound(client: TestClient):
    client.app.state.config.device_session.unbound_device_policy = "dev_direct_voice"
    dm = client.app.state.device_manager
    key = ec.generate_private_key(ec.SECP256R1())
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-4", "pending-jwt"),
    ):
        r0 = client.get(
            "/api/config",
            headers=_signed_device_headers(device_id="dev-4", key=key),
        )
    assert r0.status_code == 200
    asyncio.run(dm.approve("dev-4"))
    client.app.state.admin_client.resolve_device.side_effect = AdminPrecondition(
        412, "device is not bound"
    )
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=("dev-4", "jwt-4"),
    ) as gen:
        r = client.get(
            "/api/config",
            params=[("client_type", "esp32")],
            headers={
                **_signed_device_headers(
                    device_id="dev-4",
                    path_query="/api/config?client_type=esp32",
                    nonce="nonce-2",
                    key=key,
                    include_public_key=False,
                ),
                "X-Device-Interaction-Mode": "full_duplex",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "active"
    assert data["config"]["room_name"].startswith("device-dev-4-")
    assert data["device"]["approved"] is True
    assert data["device"]["bound"] is False
    voice_meta = gen.call_args_list[0].kwargs["participant_metadata"]
    assert voice_meta["kind"] == "device"
    assert voice_meta["interaction_mode"] == "full_duplex"


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


def test_device_session_policy_defaults_to_pending_only():
    cfg = _device_session_from_yaml({})
    assert cfg.unbound_device_policy == "pending_only"


def test_device_session_policy_accepts_dev_direct_voice():
    cfg = _device_session_from_yaml(
        {"device_session": {"unbound_device_policy": "dev_direct_voice"}}
    )
    assert cfg.unbound_device_policy == "dev_direct_voice"


def test_device_session_policy_rejects_unknown_value():
    with pytest.raises(ValueError, match="unbound_device_policy"):
        _device_session_from_yaml({"device_session": {"unbound_device_policy": "open"}})


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
