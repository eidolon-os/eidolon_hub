"""Hub-owned adapter for the Admin runtime HTTP contracts."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict


class AdminClientError(Exception):
    pass


class AdminNotFound(AdminClientError):
    pass


class AdminPrecondition(AdminClientError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AdminUpstreamError(AdminClientError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"admin upstream {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class AdminUnreachable(AdminClientError):
    pass


class AdminResolveError(AdminClientError):
    pass


class AdminResolveNotFound(AdminResolveError):
    pass


class AdminResolvePrecondition(AdminResolveError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AdminResolveUpstream(AdminResolveError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"admin upstream {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class AdminResolveUnreachable(AdminResolveError):
    pass


class ResolvedContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str
    genome_hash: str
    realizer_version: str
    device_id: str | None = None
    interaction_mode: str | None = None

    @classmethod
    def from_resolve_response(cls, data: dict[str, Any]) -> "ResolvedContext":
        context = data.get("context")
        if not isinstance(context, dict):
            raise ValueError("admin resolve response missing context")
        return cls.model_validate(context)


def unwrap_detail(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)
    return body


class _AdminHTTPBase:
    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        self._http = http_client
        self._base = base_url.rstrip("/")

    async def _get_json(
        self,
        path: str,
        *,
        precondition_exc: type[AdminPrecondition] = AdminPrecondition,
        not_found_exc: type[Exception] = AdminNotFound,
        upstream_exc: type[AdminUpstreamError] = AdminUpstreamError,
        unreachable_exc: type[Exception] = AdminUnreachable,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request("GET", f"{self._base}{path}", timeout=5.0)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise unreachable_exc(f"admin GET {path} failed: {exc}") from exc
        if response.status_code >= 400:
            message = unwrap_detail(response.text)
            if response.status_code == 404:
                raise not_found_exc(message)
            if response.status_code in (409, 412):
                raise precondition_exc(response.status_code, message)
            raise upstream_exc(response.status_code, message)
        payload = response.json()
        if not isinstance(payload, dict):
            raise upstream_exc(response.status_code, "admin response must be an object")
        return payload


class AdminClient(_AdminHTTPBase):
    async def get_owner(self, owner_id: str) -> dict[str, Any]:
        return await self._get_json(f"/api/owners/{quote(owner_id, safe='')}")


class AdminResolveClient(_AdminHTTPBase):
    async def resolve_owner(self, owner_id: str) -> ResolvedContext:
        data = await self._get_json(
            f"/api/resolve/owner/{quote(owner_id, safe='')}",
            precondition_exc=AdminResolvePrecondition,
            not_found_exc=AdminResolveNotFound,
            upstream_exc=AdminResolveUpstream,
            unreachable_exc=AdminResolveUnreachable,
        )
        return ResolvedContext.from_resolve_response(data)

    async def resolve_device(self, device_id: str) -> ResolvedContext:
        data = await self._get_json(
            f"/api/resolve/device/{quote(device_id, safe='')}",
            precondition_exc=AdminResolvePrecondition,
            not_found_exc=AdminResolveNotFound,
            upstream_exc=AdminResolveUpstream,
            unreachable_exc=AdminResolveUnreachable,
        )
        return ResolvedContext.from_resolve_response(data)
