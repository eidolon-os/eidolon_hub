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
        audience: str = "eidolon-admission",
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
            exact_read = (
                permission is ManagementPermission.DEVICE_GET
                and owner_scope is not None
                and device_id is not None
            )
            claim_event_read = permission is ManagementPermission.CLAIM_EVENTS
            if not exact_read and not claim_event_read:
                raise PermissionError(
                    "device registry reader is limited to exact reads and Claim events"
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
        scopes_raw = claims.get("scopes") or []
        scopes = (
            {scope for item in scopes_raw if (scope := str(item).strip())}
            if isinstance(scopes_raw, list)
            else set()
        )
        subject_id = str(claims.get("sub") or "").strip()
        if not subject_id or len(subject_id) > 255:
            raise PermissionError("management credential subject is invalid")

        if owner_scope is not None and not is_admin:
            if not claim_owner or claim_owner != owner_scope:
                raise PermissionError("management credential owner scope mismatch")

        required_scope = {
            ManagementPermission.DEVICE_LIST: "device.read",
            ManagementPermission.DEVICE_GET: "device.read",
            ManagementPermission.DEVICE_EVENTS: "device.events.read",
            ManagementPermission.DEVICE_CONTROL_GET: "device.claim.revoke",
            ManagementPermission.DEVICE_APPROVE: "device.claim.approve",
            ManagementPermission.DEVICE_REVOKE: "device.claim.revoke",
            # Named because it exists: the canonical ActorRef scope for reading
            # the Claim stream, which the router's own actor path already
            # requires. Leaving it unmapped refused every JWT principal with a
            # message that named no scope at all, so the two ways into this
            # route disagreed about what authorizes it.
            ManagementPermission.CLAIM_EVENTS: "device.claim.events.read",
        }.get(permission)
        if not is_admin and required_scope not in scopes:
            raise PermissionError(f"management credential lacks {required_scope} scope")

        target_device_id = str(claims.get("target_device_id") or "") or None
        if not is_admin and target_device_id is not None and target_device_id != device_id:
            raise PermissionError("management credential target device mismatch")
        presenter = str(claims.get("presenter") or "").strip()
        if not is_admin and presenter and presenter != subject_id:
            raise PermissionError("management credential presenter mismatch")
        owner_generation = claims.get("target_owner_domain_generation")
        generation = claims.get("target_claim_generation")
        trust_epoch = claims.get("target_trust_epoch")
        manifest_digest = str(claims.get("target_manifest_digest") or "") or None
        if owner_generation is not None and (
            not isinstance(owner_generation, int)
            or isinstance(owner_generation, bool)
            or owner_generation < 1
        ):
            raise PermissionError("management credential Owner generation is invalid")
        if generation is not None and (not isinstance(generation, int) or generation < 1):
            raise PermissionError("management credential Claim generation is invalid")
        if trust_epoch is not None and (not isinstance(trust_epoch, int) or trust_epoch < 1):
            raise PermissionError("management credential trust epoch is invalid")
        if manifest_digest is not None and (
            len(manifest_digest) != 71
            or not manifest_digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in manifest_digest[7:])
        ):
            raise PermissionError("management credential manifest digest is invalid")

        if (
            permission
            in {
                ManagementPermission.DEVICE_APPROVE,
                ManagementPermission.DEVICE_REVOKE,
            }
            and not is_admin
        ):
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
            scopes=frozenset(scopes),
            actor_ref=(str(claims.get("actor_ref") or "").strip() or None),
            intent_id=(str(claims.get("intent_id") or "").strip() or None),
            target_device_id=target_device_id,
            target_owner_domain_generation=owner_generation,
            target_claim_generation=generation,
            target_trust_epoch=trust_epoch,
            target_manifest_digest=manifest_digest,
        )
