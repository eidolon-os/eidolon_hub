"""Constant-time static bearer authorization for local/admin deployments."""

from __future__ import annotations

import hmac


class StaticBearerManagementAuthorizer:
    def __init__(self, token: str) -> None:
        if len(token.encode()) < 32:
            raise ValueError("management bearer token must contain at least 32 bytes")
        self._token = token

    async def authorize(
        self, *, credential: str, owner_scope: str | None, device_id: str | None
    ) -> None:
        scheme, separator, value = credential.partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(value, self._token)
        ):
            raise PermissionError("invalid management credential")
