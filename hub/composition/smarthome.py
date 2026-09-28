"""Compose the built-in virtual provider; business logic stays in runtime."""

from pathlib import Path

from hub.smarthome.http_registry import HttpRegistrySource
from hub.smarthome.runtime import SmartHomeRuntime
from hub.smarthome.virtual import VirtualProvider


def build_smarthome(config, client, environ):
    if config.smarthome.workspace_url is None:
        return None
    for key in ("EIDOLON_HUB_SMARTHOME_TOKEN", "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN"):
        if len(environ.get(key, "")) < 32:
            raise RuntimeError(f"{key} must contain at least 32 characters")
    path = Path(
        config.smarthome.state_path or Path(config.persistence.path).with_name("smarthome.sqlite3")
    )
    path = path.expanduser()
    legacy = Path(config.persistence.path).expanduser().parent.parent / "channel/smarthome.sqlite3"
    if config.smarthome.state_path is None and legacy.exists() and not path.exists():
        raise RuntimeError(
            "existing smart-home state requires offline migration from Channel to Hub"
        )
    provider = VirtualProvider(path)
    provider.initialize()
    return SmartHomeRuntime(
        registry=HttpRegistrySource(
            client,
            config.smarthome.workspace_url,
            environ["EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN"],
        ),
        providers={"virtual": provider},
    )
