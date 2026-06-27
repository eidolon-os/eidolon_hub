"""Hub unified config — structured settings in YAML, secrets in config/.env."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_YAML = _REPO_ROOT / "config" / "settings.yaml"
_LEGACY_ENV = _REPO_ROOT / ".env"
_DEFAULT_ENV = _REPO_ROOT / "config" / ".env"


def _resolve_settings_yaml() -> Path:
    explicit = os.environ.get("EIDOLON_HUB_SETTINGS_YAML", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"EIDOLON_HUB_SETTINGS_YAML missing: {p}")
        return p.resolve()
    if _DEFAULT_YAML.is_file():
        return _DEFAULT_YAML.resolve()
    raise FileNotFoundError(
        f"hub settings not found: {_DEFAULT_YAML}. Run ./scripts/init-config.sh"
    )


def _resolve_env_file() -> Path:
    explicit = os.environ.get("EIDOLON_HUB_ENV_FILE", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"EIDOLON_HUB_ENV_FILE missing: {p}")
        return p.resolve()
    if _DEFAULT_ENV.is_file():
        return _DEFAULT_ENV.resolve()
    if _LEGACY_ENV.is_file():
        return _LEGACY_ENV.resolve()
    raise FileNotFoundError(
        f"hub env not found: {_DEFAULT_ENV}. Run ./scripts/init-config.sh"
    )


def _bootstrap_dotenv() -> None:
    from dotenv import load_dotenv

    load_dotenv(_resolve_env_file(), override=False)


def _load_yaml() -> dict[str, Any]:
    data = yaml.safe_load(_resolve_settings_yaml().read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("hub settings.yaml must be a mapping")
    return data


def _yaml_secret_value(section: dict[str, Any], field: str, env_var: str) -> str:
    """Read secret from env; yaml may use ``env_var`` as placeholder."""
    val = str(section.get(field) or "").strip()
    if not val or val == env_var:
        return os.environ.get(env_var, "").strip()
    raise ValueError(
        f"livekit.{field} must be empty or the placeholder {env_var}; "
        f"set {env_var} in config/.env"
    )


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    sec = data.get(key) or {}
    return sec if isinstance(sec, dict) else {}


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8082


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class LiveKitConfig:
    """Hub's server-side LiveKit API role: room management + data injection.

    Channel uses the same server/key/secret via its own worker config fields.
    This project deliberately has no shared LiveKitProfile abstraction yet.
    """

    api_url: str = ""
    api_key: str = ""
    api_secret: str = ""


@dataclass
class Esp32Config:
    livekit_url: str = ""
    livekit_ip: str = "auto"
    livekit_port: int = 7880
    livekit_scheme: str = "ws"


@dataclass
class DiscoveryConfig:
    service_type: str = "_eidolon-hub._tcp.local."
    service_name: str = ""
    hostname: str = "eidolon-hub"
    txt_version: str = "1"
    api_version: str = "v1"
    config_path: str = "/api/config"


@dataclass
class AdminConfig:
    probe_enabled: bool = True
    probe_interval_seconds: int = 10
    offline_after_missed_probes: int = 3
    degraded_after_missed_probes: int = 2
    command_timeout_seconds: int = 30


@dataclass
class ControlBridgeConfig:
    enabled: bool = False
    livekit_url: str = ""
    identity_prefix: str = "eidolon-hub-control"


@dataclass
class RuntimeAdminConfig:
    """Phase 32.A: hub queries admin to validate ``user_id`` at
    ``/api/config`` time. We follow LiveKit's "trust participant.identity,
    look up the rest server-side" pattern — hub mints the LK token but
    does NOT sign any device JWT here. channel does the runtime token
    signing under plan D, using the PAIRING_JWT_SECRET it shares with
    eidolon-agent.

    Phase 33.A6: removed the ``enabled=false`` rollback flag. Channel
    already deleted its static-token fallback in 32.D, so any hub-side
    bypass would only mint LK tokens that channel will immediately
    reject at admin /api/resolve. Removing it forces a single,
    consistent runtime path through admin.

    Hub consumes Admin's resolved business context; it does not store device
    bindings or own agent metadata.
    """

    admin_api_url: str = "http://127.0.0.1:9000"


@dataclass
class ProactiveWakeConfig:
    """Phase 3: hub subscribes to the agent's proactive-trigger events on NATS
    and wakes the target device via a room.join control command. Pure router —
    the publisher (agent) stamps device_id + full payload into the event; hub
    does no instance->device mapping. Opt-in (needs a running NATS)."""

    enabled: bool = False
    nats_url: str = "nats://127.0.0.1:4222"
    wake_subject: str = "agent.proactive.triggered.*"
    command_ttl_ms: int = 30_000


DEVICE_SESSION_POLICY_PENDING_ONLY = "pending_only"
DEVICE_SESSION_POLICY_DEV_DIRECT_VOICE = "dev_direct_voice"
VALID_DEVICE_SESSION_POLICIES = {
    DEVICE_SESSION_POLICY_PENDING_ONLY,
    DEVICE_SESSION_POLICY_DEV_DIRECT_VOICE,
}


@dataclass
class DeviceSessionConfig:
    unbound_device_policy: str = DEVICE_SESSION_POLICY_PENDING_ONLY


def _outbound_ipv4() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _is_loopback_request_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return True
    if h == "localhost":
        return True
    if h in {"127.0.0.1", "::1"}:
        return True
    if h.startswith("127."):
        return True
    return False


def _ws_scheme_from_config_and_request(
    configured: str,
    hub_request_scheme: str,
    *,
    explicit_host: bool,
) -> str:
    if configured in ("ws", "wss"):
        return configured
    if explicit_host:
        return "ws"
    return "wss" if hub_request_scheme == "https" else "ws"


def resolve_eidolon_livekit_client_url(
    esp32: Esp32Config,
    *,
    request_host: str,
    request_scheme: str,
) -> str:
    if esp32.livekit_url:
        return esp32.livekit_url

    host_part = esp32.livekit_ip.strip()
    if host_part and host_part.lower() != "auto":
        scheme = _ws_scheme_from_config_and_request(
            esp32.livekit_scheme,
            request_scheme,
            explicit_host=True,
        )
        return f"{scheme}://{host_part}:{esp32.livekit_port}"

    hub_host = (request_host or "").strip()
    if _is_loopback_request_host(hub_host):
        lan = _outbound_ipv4()
        if lan == "127.0.0.1":
            raise ValueError(
                "Cannot resolve a LAN-reachable LiveKit URL: outbound IPv4 is "
                "127.0.0.1. Set esp32.livekit_url or esp32.livekit_ip in settings.yaml."
            )
        hub_host = lan

    scheme = _ws_scheme_from_config_and_request(
        esp32.livekit_scheme,
        request_scheme,
        explicit_host=False,
    )
    return f"{scheme}://{hub_host}:{esp32.livekit_port}"


def _api_from_yaml(y: dict[str, Any]) -> ApiConfig:
    sec = _section(y, "api")
    return ApiConfig(
        host=str(sec.get("host", "0.0.0.0")),
        port=int(sec.get("port", 8082)),
    )


def _logging_from_yaml(y: dict[str, Any]) -> LoggingConfig:
    sec = _section(y, "logging")
    return LoggingConfig(level=str(sec.get("level", "INFO")))


def _livekit_from_yaml_and_env(y: dict[str, Any]) -> LiveKitConfig:
    sec = _section(y, "livekit")
    yaml_key = str(sec.get("api_key") or "").strip()
    yaml_secret = str(sec.get("api_secret") or "").strip()
    for val, field_name, env_var in (
        (yaml_key, "api_key", "LIVEKIT_API_KEY"),
        (yaml_secret, "api_secret", "LIVEKIT_API_SECRET"),
    ):
        if val and val != env_var:
            raise ValueError(
                f"livekit.{field_name} must be empty or the placeholder {env_var}; "
                f"set {env_var} in config/.env"
            )
    return LiveKitConfig(
        api_url=str(sec.get("api_url") or "").strip(),
        api_key=_yaml_secret_value(sec, "api_key", "LIVEKIT_API_KEY"),
        api_secret=_yaml_secret_value(sec, "api_secret", "LIVEKIT_API_SECRET"),
    )


def _esp32_from_yaml(y: dict[str, Any]) -> Esp32Config:
    sec = _section(y, "esp32")
    return Esp32Config(
        livekit_url=str(sec.get("livekit_url") or "").strip(),
        livekit_ip=str(sec.get("livekit_ip", "auto")).strip(),
        livekit_port=int(sec.get("livekit_port", 7880)),
        livekit_scheme=str(sec.get("livekit_scheme", "ws")).strip().lower(),
    )


def _discovery_from_yaml(y: dict[str, Any]) -> DiscoveryConfig:
    sec = _section(y, "mdns")
    if not sec:
        sec = _section(y, "discovery")
    return DiscoveryConfig(
        service_type=str(sec.get("service_type", "_eidolon-hub._tcp.local.")),
        service_name=str(sec.get("service_name") or ""),
        hostname=str(sec.get("hostname", "eidolon-hub")),
        txt_version=str(sec.get("txt_version", "1")),
        api_version=str(sec.get("api_version", "v1")),
        config_path=str(sec.get("config_path", "/api/config")),
    )


def _runtime_admin_from_yaml_and_env(y: dict[str, Any]) -> RuntimeAdminConfig:
    """Load ``runtime_admin`` section.

    ``admin_api_url`` may be a literal URL or an env var name (matching
    hub's livekit/secret-placeholder convention) — if no scheme, treat
    it as an env var to look up.
    """
    sec = _section(y, "runtime_admin")
    # backward compat: older yaml used the ``runtime_tokens`` block name
    # before plan D moved token signing out of hub.
    if not sec:
        sec = _section(y, "runtime_tokens")

    yaml_url = str(sec.get("admin_api_url") or "").strip()
    if yaml_url and not yaml_url.startswith(("http://", "https://")):
        yaml_url = os.environ.get(yaml_url, "").strip()
    admin_url = yaml_url or os.environ.get(
        "EIDOLON_ADMIN_API_URL", "http://127.0.0.1:9000"
    )

    # Phase 33.A6: ``enabled`` was removed from RuntimeAdminConfig.
    # If a legacy yaml still has ``enabled: false``, the loader silently
    # ignores it — the runtime is unconditional now.
    return RuntimeAdminConfig(admin_api_url=admin_url)


def _admin_from_yaml(y: dict[str, Any]) -> AdminConfig:
    sec = _section(y, "admin_probe")
    if not sec:
        sec = _section(y, "admin")
    return AdminConfig(
        probe_enabled=bool(sec.get("enabled", sec.get("probe_enabled", True))),
        probe_interval_seconds=int(
            sec.get("interval_seconds", sec.get("probe_interval_seconds", 10))
        ),
        offline_after_missed_probes=int(
            sec.get("offline_after_missed_probes", 3)
        ),
        degraded_after_missed_probes=int(
            sec.get("degraded_after_missed_probes", 2)
        ),
        command_timeout_seconds=int(
            sec.get("command_timeout_seconds", 30)
        ),
    )


def _control_bridge_from_yaml(y: dict[str, Any]) -> ControlBridgeConfig:
    sec = _section(y, "control_bridge")
    return ControlBridgeConfig(
        enabled=bool(sec.get("enabled", False)),
        livekit_url=str(sec.get("livekit_url") or "").strip(),
        identity_prefix=str(sec.get("identity_prefix", "eidolon-hub-control")).strip()
        or "eidolon-hub-control",
    )


def _proactive_wake_from_yaml(y: dict[str, Any]) -> ProactiveWakeConfig:
    sec = _section(y, "proactive_wake")
    default = ProactiveWakeConfig()
    nats_url = (os.getenv("NATS_URL") or str(sec.get("nats_url") or "")).strip()
    return ProactiveWakeConfig(
        enabled=bool(sec.get("enabled", default.enabled)),
        nats_url=nats_url or default.nats_url,
        wake_subject=str(sec.get("wake_subject") or default.wake_subject).strip()
        or default.wake_subject,
        command_ttl_ms=int(sec.get("command_ttl_ms", default.command_ttl_ms)),
    )


def _device_session_from_yaml(y: dict[str, Any]) -> DeviceSessionConfig:
    sec = _section(y, "device_session")
    raw = str(
        sec.get("unbound_device_policy", DEVICE_SESSION_POLICY_PENDING_ONLY)
    ).strip().lower()
    if raw not in VALID_DEVICE_SESSION_POLICIES:
        allowed = ", ".join(sorted(VALID_DEVICE_SESSION_POLICIES))
        raise ValueError(
            "device_session.unbound_device_policy must be one of: "
            f"{allowed}; got {raw!r}"
        )
    return DeviceSessionConfig(unbound_device_policy=raw)


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    livekit: LiveKitConfig = field(default_factory=LiveKitConfig)
    esp32: Esp32Config = field(default_factory=Esp32Config)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    control_bridge: ControlBridgeConfig = field(default_factory=ControlBridgeConfig)
    runtime_admin: RuntimeAdminConfig = field(default_factory=RuntimeAdminConfig)
    proactive_wake: ProactiveWakeConfig = field(default_factory=ProactiveWakeConfig)
    device_session: DeviceSessionConfig = field(default_factory=DeviceSessionConfig)

    @classmethod
    def load(cls) -> "AppConfig":
        _bootstrap_dotenv()
        y = _load_yaml()
        return cls(
            api=_api_from_yaml(y),
            logging=_logging_from_yaml(y),
            livekit=_livekit_from_yaml_and_env(y),
            esp32=_esp32_from_yaml(y),
            discovery=_discovery_from_yaml(y),
            admin=_admin_from_yaml(y),
            control_bridge=_control_bridge_from_yaml(y),
            runtime_admin=_runtime_admin_from_yaml_and_env(y),
            proactive_wake=_proactive_wake_from_yaml(y),
            device_session=_device_session_from_yaml(y),
        )


def load_config() -> AppConfig:
    return AppConfig.load()
