"""A stand-in for the 周边好生活 cloud that speaks protocol v1.0.5.

Used by the adapter's tests in-process and by the demo as a service on the Host
until the platform hands over a test environment; then only the base URL and
the credentials change. It checks every signature with the shared protocol
functions, so a wrong signature fails here first.

It understands a handful of Chinese instructions well enough for a demo:
打开/关闭/开/关 + a device name (or a room + product name), and refuses the
rest with the protocol's status codes. State is in memory and shown in its
``/mock/state`` endpoint for the acceptance script.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import protocol as p


@dataclass
class MockDevice:
    device_id: str
    resource_id: str | None
    product_id: str
    product_name: str
    device_name: str
    place: str
    connected: bool = True
    on: bool = False


@dataclass
class MockTenant:
    """One appId: its secret, its known accounts, and the homes behind them."""

    app_id: str
    app_secret: str
    phones: set[str]
    user_secrets: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, tuple[str, float]] = field(default_factory=dict)  # access -> (user, expiry)
    refresh: dict[str, str] = field(default_factory=dict)  # refresh -> user
    homes: dict[str, str] = field(default_factory=dict)  # homeId -> name
    devices: dict[str, list[MockDevice]] = field(default_factory=dict)  # homeId -> devices
    scopes: dict[str, str] = field(default_factory=dict)  # access -> scope


DEFAULT_APP_ID = "pEOHuobHzgkkqVZQ"
DEFAULT_APP_SECRET = "3LA3PK2OKdsi+MZoVuOew1ANkTqYBRjJ"
DEFAULT_PHONE = "13800000000"
ACCESS_TTL_S = 7199
REFRESH_TTL_S = 43199


def demo_tenant() -> MockTenant:
    tenant = MockTenant(DEFAULT_APP_ID, DEFAULT_APP_SECRET, {DEFAULT_PHONE})
    tenant.homes = {"hxxx01": "我的家"}
    tenant.devices["hxxx01"] = [
        MockDevice(
            "ATARS1Bj0001D83BDA303CD8", "property.power1", "PVIG069E", "开关", "主卧吸顶灯", "主卧"
        ),
        MockDevice(
            "ATARS1Bj0001D83BDA303CD8", "property.power2", "PVIG069E", "开关", "客厅筒灯", "客厅"
        ),
        MockDevice("ATARS1Bj0001xxxxxxxxx", None, "ABCCCCC", "开关", "主卧窗帘", "主卧"),
        MockDevice(
            "ATARS1Bj0002AC00000001", None, "O0EQQ701", "空调", "客厅空调", "客厅", connected=False
        ),
    ]
    return tenant


def create_app(tenant: MockTenant | None = None, *, clock=time.time) -> FastAPI:
    tenant = tenant or demo_tenant()
    app = FastAPI(title="周边好生活 cloud mock")
    app.state.tenant = tenant
    app.state.calls: list[dict[str, Any]] = []

    def envelope(code: int, data: Any = None, msg: str = "") -> JSONResponse:
        return JSONResponse({"code": code, "msg": msg, "data": data})

    def record(path: str, body: Any) -> None:
        app.state.calls.append({"path": path, "body": body, "at": clock()})

    def fresh(timestamp: int) -> bool:
        return abs(clock() - timestamp) < 300

    @app.post(p.PATH_VALID)
    async def valid(request: Request):
        body = await request.json()
        record(p.PATH_VALID, body)
        if body.get("appId") != tenant.app_id:
            return envelope(403, msg="无操作权限")
        expected = p.sign_valid(
            tenant.app_secret,
            app_id=body["appId"],
            nonce=body["nonce"],
            timestamp=int(body["timestamp"]),
            union_id=body["unionId"],
        )
        if expected != body.get("sign") or not fresh(int(body["timestamp"])):
            return envelope(403, msg="签名错误")
        return envelope(0, body["unionId"] in tenant.phones)

    @app.post(p.PATH_ACTIVE)
    async def active(request: Request):
        body = await request.json()
        record(p.PATH_ACTIVE, body)
        if body.get("appId") != tenant.app_id:
            return envelope(403, msg="无操作权限")
        expected = p.sign_active(
            tenant.app_secret,
            app_id=body["appId"],
            timestamp=int(body["timestamp"]),
            union_id=body["unionId"],
            user_id=body["userId"],
        )
        if expected != body.get("sign"):
            return envelope(403, msg="签名错误")
        if body["unionId"] not in tenant.phones:
            return envelope(404, msg="账号不存在")
        user_id = body["userId"]
        secret = base64_secret()
        tenant.user_secrets[user_id] = secret
        return envelope(0, {"userId": user_id, "userSecret": secret})

    async def token_endpoint(request: Request, authorization: str | None):
        # Parsed by hand: the real platform takes x-www-form-urlencoded and this
        # mock must not pull python-multipart into the Hub for it.
        raw = (await request.body()).decode()
        form = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        record(p.PATH_TOKEN, {k: v for k, v in form.items() if k != "password"})
        if authorization != p.basic_authorization(tenant.app_id, tenant.app_secret):
            return envelope(401, msg="账号未登录")
        grant = form.get("grant_type")
        if grant == p.GRANT_REFRESH:
            user = tenant.refresh.pop(form.get("refresh_token", ""), None)
            if user is None:
                return envelope(401, msg="refresh_token 无效")
            scope = form.get("scope", "")
            return envelope(0, issue(user, scope))
        if grant != p.GRANT_HMAC:
            return envelope(400, msg="grant_type 不支持")
        app_id, _, user_id = form.get("username", "").partition(".")
        secret = tenant.user_secrets.get(user_id)
        if app_id != tenant.app_id or secret is None:
            return envelope(401, msg="账号未登录")
        fields = dict(
            part.split("=", 1) for part in form.get("password", "").split("&") if "=" in part
        )
        try:
            timestamp = int(fields["timestamp"])
        except (KeyError, ValueError):
            return envelope(400, msg="password 格式错误")
        expected = p.sign_password(secret, app_id=app_id, timestamp=timestamp, user_id=user_id)
        if unquote(fields.get("sign", "")) != expected or fields.get("secureMode") != p.GRANT_HMAC:
            return envelope(401, msg="签名错误")
        scope = form.get("scope", "")
        return envelope(0, issue(user_id, scope))

    def issue(user_id: str, scope: str) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        tenant.tokens[access] = (user_id, clock() + ACCESS_TTL_S)
        tenant.refresh[refresh] = user_id
        tenant.scopes[access] = scope
        return {
            "token_type": "bearer",
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": ACCESS_TTL_S,
            "refresh_expires_in": REFRESH_TTL_S,
            "scope": scope,
            "accountId": next(iter(tenant.phones)),
        }

    @app.post(p.PATH_TOKEN)
    async def token(request: Request, authorization: str | None = Header(default=None)):
        return await token_endpoint(request, authorization)

    @app.post(p.PATH_REFRESH)
    async def refresh(request: Request, authorization: str | None = Header(default=None)):
        return await token_endpoint(request, authorization)

    def bearer(authorization: str | None) -> tuple[str, str] | JSONResponse:
        token = (authorization or "").removeprefix("Bearer ").strip()
        entry = tenant.tokens.get(token)
        if entry is None:
            return envelope(401, msg="账号未登录")
        user, expiry = entry
        if clock() > expiry:
            return envelope(p.CODE_INVALID_TOKEN, msg="无效 access_token")
        return user, tenant.scopes.get(token, "")

    @app.get(p.PATH_HOMES)
    async def homes(authorization: str | None = Header(default=None)):
        auth = bearer(authorization)
        if isinstance(auth, JSONResponse):
            return auth
        return envelope(0, [{"homeId": k, "name": v} for k, v in tenant.homes.items()])

    @app.get(p.PATH_DEVICES)
    async def devices(homeId: str, authorization: str | None = Header(default=None)):
        auth = bearer(authorization)
        if isinstance(auth, JSONResponse):
            return auth
        if homeId not in tenant.devices:
            return envelope(404, msg="请求未找到")
        return envelope(
            0,
            [
                {
                    "productId": d.product_id,
                    "productName": d.product_name,
                    "place": d.place,
                    "deviceId": d.device_id,
                    "deviceName": d.device_name,
                    "resourceId": d.resource_id,
                    "connected": d.connected,
                }
                for d in tenant.devices[homeId]
            ],
        )

    @app.get(p.PATH_PRODUCTS)
    async def products(request: Request, authorization: str | None = Header(default=None)):
        auth = bearer(authorization)
        if isinstance(auth, JSONResponse):
            return auth
        wanted = set(
            request.query_params.getlist("productIds")
            + request.query_params.getlist("productIds[]")
        )
        classify = {"开关": "switch", "空调": "ac"}
        seen: dict[str, dict[str, Any]] = {}
        for devices in tenant.devices.values():
            for d in devices:
                if not wanted or d.product_id in wanted:
                    seen[d.product_id] = {
                        "productId": d.product_id,
                        "name": d.product_name,
                        "pictureUrl": "http://xxx.png",
                        "classifyName": classify.get(d.product_name, "other"),
                        "supplierName": "aqara",
                    }
        return envelope(0, list(seen.values()))

    @app.post(p.PATH_CONTROL)
    async def control(request: Request, authorization: str | None = Header(default=None)):
        auth = bearer(authorization)
        if isinstance(auth, JSONResponse):
            return auth
        _user, scope = auth
        if p.SCOPE_CONTROL not in scope.split(","):
            return envelope(403, msg="无操作权限")
        body = await request.json()
        record(p.PATH_CONTROL, body)
        query = str(body.get("query", ""))
        home_id = body.get("homeId") or next(iter(tenant.homes), None)
        devices = tenant.devices.get(home_id, [])
        if not devices:
            return envelope(
                0, {"status": p.CONTROL_NO_DEVICES_BOUND, "answer": "抱歉，您目前还没有绑定设备"}
            )
        turn_on = any(word in query for word in ("打开", "开启", "开一下", "开灯"))
        turn_off = any(word in query for word in ("关闭", "关掉", "关上", "关灯"))
        if not (turn_on or turn_off) and query.startswith(("开", "关")):
            turn_on, turn_off = query.startswith("开"), query.startswith("关")
        targets = [d for d in devices if d.device_name and d.device_name in query]
        if not targets:
            targets = [d for d in devices if d.place in query and d.product_name in query]
        if not targets:
            return envelope(
                0,
                {
                    "status": p.CONTROL_NO_MATCH,
                    "answer": f"账户{next(iter(tenant.phones))}没有匹配到设备",
                },
            )
        if not (turn_on or turn_off):
            return envelope(0, {"status": p.CONTROL_NO_REPLY, "answer": ""})
        offline = [d for d in targets if not d.connected]
        if offline and len(offline) == len(targets):
            return envelope(0, {"status": p.CONTROL_DEVICE_OFFLINE, "answer": "设备已离线"})
        for d in targets:
            if d.connected:
                d.on = turn_on
        if offline:
            return envelope(
                0, {"status": p.CONTROL_SOME_OFFLINE, "answer": f"{len(offline)}台设备已离线"}
            )
        verb = "打开" if turn_on else "关闭"
        names = "、".join(d.device_name for d in targets)
        return envelope(0, {"status": p.CONTROL_OK, "answer": f"好的，为您{verb}{names}"})

    @app.get("/mock/state")
    async def state():
        return {
            home: [
                {
                    "deviceName": d.device_name,
                    "place": d.place,
                    "connected": d.connected,
                    "on": d.on,
                }
                for d in devices
            ]
            for home, devices in tenant.devices.items()
        }

    @app.post("/mock/connected")
    async def set_connected(request: Request):
        body = await request.json()
        for devices in tenant.devices.values():
            for d in devices:
                if d.device_name == body["deviceName"]:
                    d.connected = bool(body["connected"])
        return {"ok": True}

    @app.get("/mock/calls")
    async def calls():
        return app.state.calls

    return app


def base64_secret() -> str:
    import base64

    return base64.b64encode(secrets.token_bytes(16)).decode()


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="周边好生活 cloud mock (protocol v1.0.5)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
