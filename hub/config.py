"""Hub unified config loader — reads exclusively from environment variables."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field


def _bootstrap_dotenv() -> None:
    from dotenv import load_dotenv

    load_dotenv()


_bootstrap_dotenv()


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8081

    @classmethod
    def from_env(cls) -> "ApiConfig":
        return cls(
            host=os.environ.get("HUB_HOST", "0.0.0.0"),
            port=int(os.environ.get("HUB_PORT", "8081")),
        )


@dataclass
class LoggingConfig:
    level: str = "INFO"

    @classmethod
    def from_env(cls) -> "LoggingConfig":
        return cls(level=os.environ.get("LOG_LEVEL", "INFO"))


@dataclass
class LiveKitConfig:
    url: str = ""
    api_key: str = ""
    api_secret: str = ""

    @classmethod
    def from_env(cls) -> "LiveKitConfig":
        explicit_url = os.environ.get("LIVEKIT_URL", "")
        fallback_url = os.environ.get("EIDOLON_LIVEKIT_URL", "")
        return cls(
            url=explicit_url or fallback_url,
            api_key=os.environ.get("LIVEKIT_API_KEY", ""),
            api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
        )


@dataclass
class Esp32Config:
    """LiveKit URL as seen by ESP32 / browsers (ws or wss).

    Resolution (see ``resolve_eidolon_livekit_client_url``):

    1. ``livekit_url`` (``EIDOLON_LIVEKIT_URL``) non-empty → use as-is.
    2. ``livekit_ip`` non-empty and not ``auto`` → ``{scheme}://{ip}:{port}``.
    3. ``livekit_ip`` empty or ``auto`` → ``{scheme}://{host}:{port}`` where
       ``host`` is the request ``Host`` if it is a non-loopback address; otherwise
       the machine's LAN IPv4 (same strategy as mDNS registration), so other
       devices never receive ``127.0.0.1`` when auto is intended for LAN access.
    """

    livekit_url: str = ""
    livekit_ip: str = ""
    livekit_port: int = 7880
    livekit_scheme: str = ""

    @classmethod
    def from_env(cls) -> "Esp32Config":
        return cls(
            livekit_url=os.environ.get("EIDOLON_LIVEKIT_URL", "").strip(),
            livekit_ip=os.environ.get("EIDOLON_LIVEKIT_IP", "").strip(),
            livekit_port=int(os.environ.get("EIDOLON_LIVEKIT_PORT", "7880")),
            livekit_scheme=os.environ.get("EIDOLON_LIVEKIT_SCHEME", "").strip().lower(),
        )


def _outbound_ipv4() -> str:
    """Return local IPv4 used for outbound UDP (typically LAN, not loopback)."""
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
    """Pick ws or wss for composed client URLs."""
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
    """Build LiveKit WebSocket URL returned in ``GET /api/config`` (esp32)."""
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
                "127.0.0.1. Set EIDOLON_LIVEKIT_URL or EIDOLON_LIVEKIT_IP to an "
                "explicit address other machines can use."
            )
        hub_host = lan

    scheme = _ws_scheme_from_config_and_request(
        esp32.livekit_scheme,
        request_scheme,
        explicit_host=False,
    )
    return f"{scheme}://{hub_host}:{esp32.livekit_port}"


@dataclass
class DiscoveryConfig:
    service_type: str = "_eidolon-hub._tcp.local."
    service_name: str = ""
    hostname: str = "eidolon-hub"
    txt_version: str = "1"
    api_version: str = "v1"
    config_path: str = "/api/config"

    @classmethod
    def from_env(cls) -> "DiscoveryConfig":
        return cls(
            service_type=os.environ.get("MDNS_SERVICE_TYPE", "_eidolon-hub._tcp.local."),
            service_name=os.environ.get("MDNS_SERVICE_NAME", ""),
            hostname=os.environ.get("MDNS_HOSTNAME", "eidolon-hub"),
            txt_version=os.environ.get("MDNS_TXT_VERSION", "1"),
            api_version=os.environ.get("MDNS_API_VERSION", "v1"),
            config_path=os.environ.get("MDNS_CONFIG_PATH", "/api/config"),
        )


@dataclass
class AdminConfig:
    probe_enabled: bool = True
    probe_interval_seconds: int = 10
    offline_after_missed_probes: int = 3
    degraded_after_missed_probes: int = 2
    command_timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "AdminConfig":
        return cls(
            probe_enabled=os.environ.get("ADMIN_PROBE_ENABLED", "true").lower() in {"1", "true", "yes"},
            probe_interval_seconds=int(os.environ.get("ADMIN_PROBE_INTERVAL_SECONDS", "10")),
            offline_after_missed_probes=int(os.environ.get("ADMIN_OFFLINE_AFTER_MISSED_PROBES", "3")),
            degraded_after_missed_probes=int(os.environ.get("ADMIN_DEGRADED_AFTER_MISSED_PROBES", "2")),
            command_timeout_seconds=int(os.environ.get("ADMIN_COMMAND_TIMEOUT_SECONDS", "30")),
        )


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    livekit: LiveKitConfig = field(default_factory=LiveKitConfig)
    esp32: Esp32Config = field(default_factory=Esp32Config)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            api=ApiConfig.from_env(),
            logging=LoggingConfig.from_env(),
            livekit=LiveKitConfig.from_env(),
            esp32=Esp32Config.from_env(),
            discovery=DiscoveryConfig.from_env(),
            admin=AdminConfig.from_env(),
        )


def load_config() -> AppConfig:
    return AppConfig.load()
