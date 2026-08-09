"""Owner-scoped JWT authorization for Hub management interfaces."""

from __future__ import annotations

import hmac

from jose import JWTError, jwt

from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.identity import ManagementPermission, ManagementPrincipal
from hub.ports.repositories import DeviceRepository


class JwtOwnerManagementAuthorizer:
    def __init__(
        self,
        *,
        secret: bytes,
        devices: DeviceRepository,
        device_registry_reader_token: str,
        audience: str = "eidolon-hub",
        issuer: str | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("management JWT secret must contain at least 32 bytes")
        if len(device_registry_reader_token.encode()) < 32:
            raise ValueError("device registry reader token must contain at least 32 bytes")
        self._secret = secret
        self._devices = devices
        self._device_registry_reader_token = device_registry_reader_token
        self._audience = audience
        self._issuer = issuer

    async def authorize(
        self,
        *,
        credential: str,
        permission: ManagementPermission,
        owner_scope: str | None,
        device_id: str | None,
    ) -> ManagementPrincipal:
        scheme, separator, token = credential.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise PermissionError("Bearer management credential required")
        if hmac.compare_digest(token, self._device_registry_reader_token):
            if (
                permission is not ManagementPermission.DEVICE_GET
                or owner_scope is None
                or device_id is None
            ):
                raise PermissionError(
                    "device registry reader is limited to exact device reads"
                )
            return ManagementPrincipal(
                subject_id="eidolon-kernel/device-authority",
                owner_id=None,
                roles=frozenset({"device-registry-reader"}),
            )
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
        roles = (
            {role for item in roles_raw if (role := str(item).strip())}
            if isinstance(roles_raw, list)
            else set()
        )
        is_admin = "hub-admin" in roles
        is_manager = "device-manager" in roles
        if not is_admin and not is_manager:
            raise PermissionError("recognized Hub management role required")
        claim_owner = str(claims.get("owner_id") or "")
        subject_id = str(claims.get("sub") or "").strip()
        if not subject_id or len(subject_id) > 255:
            raise PermissionError("management credential subject is invalid")

        if owner_scope is not None and not is_admin:
            if not claim_owner or claim_owner != owner_scope:
                raise PermissionError("management credential owner scope mismatch")

        if permission in {
            ManagementPermission.DEVICE_APPROVE,
            ManagementPermission.DEVICE_REVOKE,
        } and not is_admin:
            if not is_manager or device_id is None:
                raise PermissionError("device-manager role required for device mutation")
            device = await self._devices.get(device_id)
            if device is None:
                raise PermissionError("managed device does not exist")
            if (
                not claim_owner
                or device.owner_id != claim_owner
                or device.lifecycle_state is not DeviceLifecycleState.APPROVED
            ):
                raise PermissionError("management credential cannot access device")
        return ManagementPrincipal(
            subject_id=subject_id,
            owner_id=claim_owner or None,
            roles=frozenset(roles),
        )
