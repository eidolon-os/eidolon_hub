"""Owner-scoped JWT authorization for Hub management interfaces."""

from __future__ import annotations

from jose import JWTError, jwt

from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.repositories import DeviceRepository


class JwtOwnerManagementAuthorizer:
    def __init__(
        self,
        *,
        secret: bytes,
        devices: DeviceRepository,
        audience: str = "eidolon-hub",
        issuer: str | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("management JWT secret must contain at least 32 bytes")
        self._secret = secret
        self._devices = devices
        self._audience = audience
        self._issuer = issuer

    async def authorize(
        self,
        *,
        credential: str,
        owner_scope: str | None,
        device_id: str | None,
    ) -> None:
        scheme, separator, token = credential.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise PermissionError("Bearer management credential required")
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require_sub": True, "require_exp": True},
            )
        except JWTError as exc:
            raise PermissionError("invalid management credential") from exc
        roles_raw = claims.get("roles") or []
        roles = {str(item) for item in roles_raw} if isinstance(roles_raw, list) else set()
        is_admin = "hub-admin" in roles
        if not is_admin and "device-manager" not in roles:
            raise PermissionError("device-manager role required")
        claim_owner = str(claims.get("owner_id") or "")
        if owner_scope is not None and not is_admin:
            if not claim_owner or claim_owner != owner_scope:
                raise PermissionError("management credential owner scope mismatch")
        if device_id is not None:
            device = await self._devices.get(device_id)
            if device is None:
                raise PermissionError("managed device does not exist")
            if not is_admin and (
                not claim_owner
                or device.owner_id != claim_owner
                or device.lifecycle_state is not DeviceLifecycleState.APPROVED
            ):
                raise PermissionError("management credential cannot access device")
