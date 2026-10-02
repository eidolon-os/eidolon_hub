"""The 周边好生活 adapter: account binding, device discovery, delegated control.

Confirmation style: delegated. The platform's ``device/llm/control`` answers in
words; this adapter returns ``Delegated(answer)`` and lets the runtime record
it as such. Reachability comes from polling the device list; state is never
reported because the protocol does not carry it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import secrets
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from eidolon_sdk.biz.smarthome import (
    ERROR_DEVICE_OFFLINE,
    ERROR_PLATFORM_REJECTED,
    ERROR_UNKNOWN_DEVICE,
    AccountSchema,
    Command,
    Device,
    DiscoveredDevice,
    Observation,
    ProviderAccount,
    SmartHomeError,
    provider_binding,
)

from hub.integration.accounts import AccountRecord, ProviderAccountStore
from hub.integration.vault import CredentialVault
from hub.smarthome.ports import BindError, Delegated

from . import protocol as p

logger = logging.getLogger("hub.smarthome.zhoubian")

KIND = "zhoubian"
POLL_INTERVAL_S = 30.0
# Refresh this far before the platform's expiry so a command never races it.
REFRESH_MARGIN = 0.1

# The platform's classifyName / productName words → SDK device types. Anything
# else is a switch: the protocol only ever lets us say on or off anyway.
_TYPE_WORDS: tuple[tuple[str, str], ...] = (
    ("空调", "climate"),
    ("ac", "climate"),
    ("窗帘", "cover"),
    ("curtain", "cover"),
    ("灯", "light"),
    ("light", "light"),
    ("风扇", "fan"),
    ("fan", "fan"),
    ("插座", "switch"),
    ("开关", "switch"),
    ("switch", "switch"),
    ("传感", "sensor"),
    ("sensor", "sensor"),
)

# What the platform can be asked for, per SDK command. Only on/off-shaped
# instructions are known to work; everything else is refused up front rather
# than sent as a sentence the platform may misread.
_VERBS: dict[tuple[str, str], str] = {
    ("on_off", "on"): "打开{name}",
    ("on_off", "off"): "关闭{name}",
    ("position", "open"): "打开{name}",
    ("position", "close"): "关闭{name}",
    ("lock", "lock"): "锁上{name}",
}


@dataclass
class _Session:
    owner_id: str
    account_id: str
    base_url: str
    app_id: str
    app_secret: str
    user_id: str
    user_secret: str
    home_id: str | None
    access_token: str = ""
    refresh_token: str = ""
    access_expires_at: float = 0.0


class ZhoubianProvider:
    """``SmartHomeProvider`` + ``ProviderIntegration`` for protocol v1.0.5."""

    kind = KIND
    pushes_observations = True

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        vault: CredentialVault,
        accounts: ProviderAccountStore,
        host_identity: str,
        clock=time.time,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._client = client
        self._vault = vault
        self._accounts = accounts
        self._host_identity = host_identity
        self._clock = clock
        self._poll_interval_s = poll_interval_s
        self._sessions: dict[str, _Session] = {}
        self._known: dict[str, dict[str, bool]] = {}  # account -> external_ref -> connected

    # --- ProviderIntegration -------------------------------------------------

    def account_schema(self) -> AccountSchema:
        return AccountSchema(
            kind=KIND,
            label="周边好生活",
            fields=(
                {"name": "base_url", "label": "平台地址", "kind": "url"},
                {"name": "app_id", "label": "appId", "kind": "text"},
                {"name": "app_secret", "label": "appSecret", "kind": "secret"},
                {"name": "phone", "label": "平台账号手机号", "kind": "phone"},
                {"name": "home_id", "label": "家庭", "kind": "choice", "required": False},
            ),
        )

    async def bind(
        self, owner_id: str, account_id: str, fields: Mapping[str, str]
    ) -> ProviderAccount:
        missing = [k for k in ("base_url", "app_id", "app_secret", "phone") if not fields.get(k)]
        if missing:
            raise BindError("MISSING_FIELDS", "缺少：" + "、".join(missing))
        session = _Session(
            owner_id=owner_id,
            account_id=account_id,
            base_url=fields["base_url"].rstrip("/"),
            app_id=fields["app_id"],
            app_secret=fields["app_secret"],
            user_id=p.derive_user_id(f"{self._host_identity}/{account_id}"),
            user_secret="",
            home_id=fields.get("home_id") or None,
        )
        try:
            base64.b64decode(session.app_secret, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BindError("INVALID_APP_SECRET", "appSecret 不是 base64") from exc
        stored_secret = await self._vault.get(account_id, "user_secret")
        if stored_secret:
            session.user_secret = stored_secret
        else:
            session.user_secret = await self._activate(session, fields["phone"])
        await self._token(session)
        homes = await self._homes(session)
        if session.home_id is None and len(homes) == 1:
            session.home_id = homes[0]["homeId"]
        for name, value in (
            ("base_url", session.base_url),
            ("app_id", session.app_id),
            ("app_secret", session.app_secret),
            ("phone", fields["phone"]),
            ("user_secret", session.user_secret),
            ("home_id", session.home_id or ""),
        ):
            await self._vault.put(account_id, name, value)
        self._sessions[account_id] = session
        label = next((h["name"] for h in homes if h["homeId"] == session.home_id), "周边好生活")
        if session.home_id is None:
            record = AccountRecord(
                account_id,
                owner_id,
                KIND,
                "周边好生活",
                "pending",
                choices=tuple({"value": h["homeId"], "label": h["name"]} for h in homes),
            )
        else:
            record = AccountRecord(
                account_id, owner_id, KIND, label, "connected", last_seen_ms=self._now_ms()
            )
        await self._accounts.upsert(record)
        return _account(record)

    async def unbind(self, owner_id: str, account_id: str) -> None:
        self._sessions.pop(account_id, None)
        self._known.pop(account_id, None)
        await self._vault.delete_all(account_id)
        await self._accounts.delete(owner_id, account_id)

    async def restore(self) -> None:
        for record in await self._accounts.all():
            if record.kind != KIND or record.status == "revoked":
                continue
            fields = {
                name: await self._vault.get(record.account_id, name) or ""
                for name in ("base_url", "app_id", "app_secret", "phone", "home_id")
            }
            try:
                await self.bind(record.owner_id, record.account_id, fields)
            except Exception as exc:  # the account stays, marked degraded
                logger.warning(
                    "zhoubian account %s could not be restored: %s", record.account_id, exc
                )
                await self._accounts.set_status(record.account_id, "degraded", error=str(exc)[:200])

    async def discover(self, owner_id: str, account_id: str) -> list[DiscoveredDevice]:
        session = self._session(owner_id, account_id)
        if session.home_id is None:
            raise BindError("HOME_NOT_CHOSEN", "请先选择家庭")
        rows = await self._devices(session)
        found: list[DiscoveredDevice] = []
        seen: dict[str, bool] = {}
        for row in rows:
            ref = external_ref(row["deviceId"], row.get("resourceId"))
            seen[ref] = bool(row.get("connected", True))
            found.append(
                DiscoveredDevice(
                    external_ref=ref,
                    name=(row.get("deviceName") or row.get("productName") or "设备")[:64],
                    suggested_type=suggest_type(
                        row.get("classifyName") or "", row.get("productName") or ""
                    ),
                    traits=("on_off",),
                    area_name=(row.get("place") or None),
                    reachable=bool(row.get("connected", True)),
                )
            )
        self._known[account_id] = seen
        return found

    async def observe(self, owner_id: str, account_id: str) -> AsyncIterator[Observation]:
        """Reachability only, from the device list, every ``poll_interval_s``."""
        while account_id in self._sessions:
            try:
                before = dict(self._known.get(account_id, {}))
                await self.discover(owner_id, account_id)
                now = self._now_ms()
                for ref, connected in self._known.get(account_id, {}).items():
                    if before.get(ref) != connected:
                        yield Observation(
                            device_id=ref, reachable=connected, state=None, observed_at_ms=now
                        )
                await self._accounts.set_status(account_id, "connected", seen=True)
            except Exception as exc:
                logger.warning("zhoubian poll failed for %s: %s", account_id, exc)
                await self._accounts.set_status(account_id, "degraded", error=str(exc)[:200])
            await asyncio.sleep(self._poll_interval_s)

    # --- SmartHomeProvider ----------------------------------------------------

    async def reconcile(self, owner_id: str, devices: Sequence[Device]) -> None:
        return None

    async def states(self, owner_id: str, devices: Sequence[Device]) -> dict[str, dict[str, Any]]:
        return {}

    async def execute(
        self, owner_id: str, device: Device, command: Command
    ) -> dict[str, Any] | Delegated:
        _kind, account_id = provider_binding(device.provider)
        session = self._session(owner_id, account_id)
        template = _VERBS.get((command.trait, command.command))
        if template is None:
            raise SmartHomeError(
                ERROR_PLATFORM_REJECTED,
                f"{command.trait}.{command.command} cannot be said to this platform",
            )
        query = template.format(name=device.name)
        answer = await self._control(session, query)
        status, text = int(answer.get("status", -1)), str(answer.get("answer") or "")
        if status == p.CONTROL_OK:
            return Delegated(text or "平台已受理")
        if status in (p.CONTROL_DEVICE_OFFLINE, p.CONTROL_SOME_OFFLINE):
            raise SmartHomeError(ERROR_DEVICE_OFFLINE, text)
        if status in (p.CONTROL_NO_DEVICES_BOUND, p.CONTROL_NO_MATCH):
            raise SmartHomeError(ERROR_UNKNOWN_DEVICE, text)
        raise SmartHomeError(ERROR_PLATFORM_REJECTED, text or f"status {status}")

    # --- protocol calls ---------------------------------------------------------

    def _session(self, owner_id: str, account_id: str) -> _Session:
        session = self._sessions.get(account_id)
        if session is None or session.owner_id != owner_id:
            raise SmartHomeError(ERROR_DEVICE_OFFLINE, f"account {account_id} is not bound")
        return session

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    async def _activate(self, session: _Session, phone: str) -> str:
        timestamp = int(self._clock())
        body = {
            "appId": session.app_id,
            "timestamp": timestamp,
            "unionId": phone,
            "accountType": p.ACCOUNT_PHONE,
            "userId": session.user_id,
            "sign": p.sign_active(
                session.app_secret,
                app_id=session.app_id,
                timestamp=timestamp,
                union_id=phone,
                user_id=session.user_id,
            ),
        }
        data = await self._post_json(session, p.PATH_ACTIVE, body, bind=True)
        secret = data.get("userSecret")
        if not secret:
            raise BindError("ACTIVATE_FAILED", "平台未返回 userSecret")
        return secret

    async def _token(self, session: _Session) -> None:
        timestamp = int(self._clock())
        form = {
            "grant_type": p.GRANT_HMAC,
            "username": p.username(session.app_id, session.user_id),
            "password": p.password(
                session.user_secret,
                app_id=session.app_id,
                timestamp=timestamp,
                user_id=session.user_id,
            ),
            "scope": p.SCOPE_CONTROL,
        }
        await self._grant(session, form)

    async def _refresh(self, session: _Session) -> None:
        if not session.refresh_token:
            await self._token(session)
            return
        try:
            await self._grant(
                session,
                {
                    "grant_type": p.GRANT_REFRESH,
                    "refresh_token": session.refresh_token,
                    "scope": p.SCOPE_CONTROL,
                },
                path=p.PATH_REFRESH,
            )
        except BindError:
            await self._token(session)

    async def _grant(
        self, session: _Session, form: dict[str, str], path: str = p.PATH_TOKEN
    ) -> None:
        response = await self._client.post(
            session.base_url + path,
            data=form,
            headers={"Authorization": p.basic_authorization(session.app_id, session.app_secret)},
            timeout=10,
        )
        data = _unwrap(response, bind=True)
        session.access_token = data["access_token"]
        session.refresh_token = data.get("refresh_token", "")
        session.access_expires_at = self._clock() + float(data.get("expires_in", 7199)) * (
            1 - REFRESH_MARGIN
        )

    async def _ensure_token(self, session: _Session) -> None:
        if not session.access_token or self._clock() >= session.access_expires_at:
            await self._refresh(session)

    async def _authed(self, session: _Session, method: str, path: str, **kwargs) -> Any:
        await self._ensure_token(session)
        for attempt in (1, 2):
            response = await self._client.request(
                method,
                session.base_url + path,
                headers={"Authorization": f"Bearer {session.access_token}"},
                timeout=10,
                **kwargs,
            )
            payload = response.json()
            if payload.get("code") in (p.CODE_UNAUTHORIZED, p.CODE_INVALID_TOKEN) and attempt == 1:
                await self._refresh(session)
                continue
            return _unwrap(response, bind=False)
        raise SmartHomeError(ERROR_PLATFORM_REJECTED, "token refresh did not take")

    async def _post_json(self, session: _Session, path: str, body: dict, *, bind: bool) -> Any:
        response = await self._client.post(session.base_url + path, json=body, timeout=10)
        return _unwrap(response, bind=bind)

    async def _homes(self, session: _Session) -> list[dict[str, Any]]:
        return list(await self._authed(session, "GET", p.PATH_HOMES) or [])

    async def _devices(self, session: _Session) -> list[dict[str, Any]]:
        return list(
            await self._authed(session, "GET", p.PATH_DEVICES, params={"homeId": session.home_id})
            or []
        )

    async def _control(self, session: _Session, query: str) -> dict[str, Any]:
        body = {"query": query}
        if session.home_id:
            body["homeId"] = session.home_id
        return dict(await self._authed(session, "POST", p.PATH_CONTROL, json=body) or {})


def _unwrap(response: httpx.Response, *, bind: bool) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise (BindError if bind else _platform_error)(
            "BAD_RESPONSE", f"HTTP {response.status_code}"
        ) from exc
    code = payload.get("code")
    if code != p.CODE_OK:
        message = f"{code}: {payload.get('msg') or ''}".strip()
        if bind:
            raise BindError("PLATFORM_REFUSED", message)
        raise SmartHomeError(ERROR_PLATFORM_REJECTED, message)
    return payload.get("data")


def _platform_error(code: str, message: str) -> SmartHomeError:
    return SmartHomeError(ERROR_PLATFORM_REJECTED, f"{code}: {message}")


def _account(record: AccountRecord) -> ProviderAccount:
    return ProviderAccount(
        account_id=record.account_id,
        kind=record.kind,
        label=record.label,
        status=record.status,
        last_seen_ms=record.last_seen_ms,
        error=record.error,
        choices=tuple(record.choices),
    )


def external_ref(device_id: str, resource_id: str | None) -> str:
    """``deviceId.resourceId`` in the SDK identifier charset; multi-key switches
    share a deviceId and differ by resource."""
    ref = f"{device_id}.{resource_id}" if resource_id else device_id
    return "".join(ch if (ch.isalnum() or ch in "._:-") else "-" for ch in ref)[:128]


def suggest_type(classify_name: str, product_name: str) -> str:
    text = f"{classify_name} {product_name}".lower()
    for word, kind in _TYPE_WORDS:
        if word in text:
            return kind
    return "switch"


def new_account_id() -> str:
    return "acc_" + secrets.token_hex(4)
