"""Drive the smart-home integration end to end against a running Hub.

    uv run python -m devtools.smarthome_demo --hub http://127.0.0.1:8082 \
        --env ../.eidolon/mac-product/config/env/hub.env --owner <owner_id> \
        bind --cloud http://127.0.0.1:8799
    ... sync | snapshot | on 主卧吸顶灯 | off 主卧吸顶灯 | receipt <request_id> | changes

Talks only to Hub's internal API with the credential in hub.env, the same way
the Agent and the Channel do. The 周边好生活 cloud mock (``python -m
hub.smarthome.providers.zhoubian.cloud_mock``) stands in for the platform
until its test environment exists; then ``--cloud`` points at the real one and
``--app-id/--app-secret/--phone`` carry its credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

from hub.smarthome.providers.zhoubian.cloud_mock import (
    DEFAULT_APP_ID,
    DEFAULT_APP_SECRET,
    DEFAULT_PHONE,
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


class Hub:
    def __init__(self, base: str, token: str, owner: str):
        self._client = httpx.AsyncClient(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=15
        )
        self._owner = owner

    async def call(self, path: str, **body):
        response = await self._client.post(
            f"/api/smarthome/v1/{path}", json={"owner_id": self._owner, **body}
        )
        if response.status_code >= 400:
            print(f"HTTP {response.status_code}: {response.text}", file=sys.stderr)
            response.raise_for_status()
        return response.json()

    async def aclose(self):
        await self._client.aclose()


def dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--hub", default="http://127.0.0.1:8082")
    parser.add_argument(
        "--env", type=Path, default=Path("../.eidolon/mac-product/config/env/hub.env")
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--account", default="acc_zhoubian_demo")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind")
    bind.add_argument("--cloud", default="http://127.0.0.1:8799")
    bind.add_argument("--app-id", default=DEFAULT_APP_ID)
    bind.add_argument("--app-secret", default=DEFAULT_APP_SECRET)
    bind.add_argument("--phone", default=DEFAULT_PHONE)
    bind.add_argument("--home-id", default="")
    sub.add_parser("providers")
    sub.add_parser("accounts")
    sub.add_parser("unbind")
    sub.add_parser("sync")
    sub.add_parser("snapshot")
    for verb in ("on", "off"):
        p = sub.add_parser(verb)
        p.add_argument("name", help="device name as shown in the registry")
    receipt = sub.add_parser("receipt")
    receipt.add_argument("request_id")
    changes = sub.add_parser("changes")
    changes.add_argument("--since", type=int, default=0)
    changes.add_argument("--timeout-ms", type=int, default=0)
    args = parser.parse_args()

    token = read_env(args.env)["EIDOLON_HUB_SMARTHOME_TOKEN"]
    hub = Hub(args.hub, token, args.owner)
    try:
        match args.command:
            case "providers":
                dump(await hub.call("providers"))
            case "accounts":
                dump(await hub.call("accounts"))
            case "bind":
                fields = {
                    "base_url": args.cloud,
                    "app_id": args.app_id,
                    "app_secret": args.app_secret,
                    "phone": args.phone,
                }
                if args.home_id:
                    fields["home_id"] = args.home_id
                dump(
                    await hub.call(
                        "accounts/bind", kind="zhoubian", account_id=args.account, fields=fields
                    )
                )
            case "unbind":
                dump(await hub.call(f"accounts/{args.account}/unbind"))
            case "sync":
                dump(await hub.call(f"accounts/{args.account}/sync"))
            case "snapshot":
                snapshot = await hub.call("snapshot")
                for device in snapshot["registry"]["devices"]:
                    status = snapshot["status"].get(device["device_id"], {})
                    print(
                        f"{device['device_id']:<40} {device['name']:<10} {device['type']:<8} "
                        f"{device['provider']:<24} online={status.get('online')} state={status.get('state')}"
                    )
            case "on" | "off":
                snapshot = await hub.call("snapshot")
                device = next(
                    (d for d in snapshot["registry"]["devices"] if d["name"] == args.name), None
                )
                if device is None:
                    print(f"no device named {args.name!r}", file=sys.stderr)
                    return 2
                request_id = f"demo:{uuid.uuid4().hex[:12]}"
                # Say it in the device's own trait: a cover opens and closes.
                if device["type"] == "cover":
                    trait, verb = "position", ("open" if args.command == "on" else "close")
                else:
                    trait, verb = "on_off", args.command
                started = time.time()
                result = await hub.call(
                    "execute",
                    request={
                        "request_id": request_id,
                        "commands": [
                            {
                                "device_id": device["device_id"],
                                "trait": trait,
                                "command": verb,
                                "params": {},
                            }
                        ],
                        "origin": {"kind": "text", "label": "demo"},
                        "deadline_ms": int(time.time() * 1000) + 3000,
                    },
                )
                print(
                    f"request_id={request_id} round-trip={int((time.time() - started) * 1000)} ms"
                )
                dump(result)
                dump(await hub.call("receipts", request_id=request_id))
            case "receipt":
                dump(await hub.call("receipts", request_id=args.request_id))
            case "changes":
                dump(await hub.call("changes", since=args.since, timeout_ms=args.timeout_ms))
    finally:
        await hub.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
