"""The one credential Hub's device-management surface still accepts."""

from __future__ import annotations

import hmac

from hub.ports.identity import ManagementPermission, ManagementPrincipal


class DeviceRegistryReaderAuthorizer:
    """Authorize Kernel's exact device reads, and nothing else.

    This surface used to accept an Owner-scoped JWT as well: roles, scopes,
    target device and target generations, about a hundred lines of it. Nothing
    minted one. Admin's only issuer of that vocabulary was deleted when the
    Claim revocation moved to the Admission credential it is actually presented
    to, and the last consumer — the device-local erase status read — now reads
    that same Admission credential. What was left authorized nobody, while
    still looking like a way in.

    So the surface is what it always effectively was: one shared reader token,
    held by Kernel, good for reading one device it names and for following the
    Claim stream. Anything wider is not refused by policy here — there is no
    longer a route to ask for it.
    """

    def __init__(self, *, device_registry_reader_token: str) -> None:
        if len(device_registry_reader_token.encode()) < 32:
            raise ValueError("device registry reader token must contain at least 32 bytes")
        self._token = device_registry_reader_token

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
        if not hmac.compare_digest(token, self._token):
            raise PermissionError("management credential is not recognized")
        exact_read = (
            permission is ManagementPermission.DEVICE_GET
            and owner_scope is not None
            and device_id is not None
        )
        if not exact_read and permission is not ManagementPermission.CLAIM_EVENTS:
            raise PermissionError(
                "device registry reader is limited to exact reads and Claim events"
            )
        return ManagementPrincipal(
            subject_id="eidolon-kernel/device-authority",
            owner_id=None,
            roles=frozenset({"device-registry-reader"}),
        )
