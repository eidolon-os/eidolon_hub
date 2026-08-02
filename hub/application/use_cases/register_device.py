"""Register identity and capabilities through an authenticated device session."""

from __future__ import annotations

import hmac
import json

from hub.domain.devices.entities import (
    DeviceRegistrationIntent,
    ManagedDevice,
)
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import (
    DeviceDirectoryProjector,
    DeviceRepository,
    DeviceSessionRepository,
)


class RegisterDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        sessions: DeviceSessionRepository,
        events: EventBus,
        clock: Clock,
        directory_projector: DeviceDirectoryProjector | None = None,
    ) -> None:
        self._devices = devices
        self._sessions = sessions
        self._events = events
        self._clock = clock
        self._directory_projector = directory_projector

    async def execute(
        self,
        *,
        session_id: str,
        lease_token: str,
        registration: DeviceRegistrationIntent,
    ) -> ManagedDevice:
        lease = await self._sessions.get(session_id)
        now = self._clock.now()
        if lease is None or not lease.is_active(now):
            raise PermissionError("active device session required")
        if (
            not hmac.compare_digest(lease.lease_token, lease_token)
            or lease.device_id != registration.identity.device_id
            or lease.identity_fingerprint != registration.identity.public_key_fingerprint
        ):
            raise PermissionError("device session does not match registration")
        current = await self._devices.get(registration.identity.device_id)
        if current is not None and current.identity != registration.identity:
            raise PermissionError("registered device identity cannot be replaced")
        if current is not None and current.last_registration_request_id == registration.request_id:
            same_request = (
                current.display_name == registration.display_name
                and current.device_kind == registration.device_kind
                and current.manifest == registration.manifest
            )
            if not same_request:
                raise ValueError("registration request_id was reused with different content")
            return current
        device = ManagedDevice(
            identity=registration.identity,
            display_name=registration.display_name,
            device_kind=registration.device_kind,
            manifest=registration.manifest,
            registered_at=current.registered_at if current else now,
            updated_at=now,
            last_registration_request_id=registration.request_id,
            owner_id=current.owner_id if current else None,
            approved=current.approved if current else False,
            revoked=current.revoked if current else False,
            last_management_request_id=(current.last_management_request_id if current else ""),
            last_management_fingerprint=(current.last_management_fingerprint if current else ""),
        )
        persisted = await self._devices.upsert(device)
        await self._events.publish(
            DomainEvent(
                event_id=registration.request_id,
                event_type="eidolon.device.registered.v1",
                source="eidolon-hub/device-management",
                subject=device.identity.device_id,
                occurred_at=now,
                data_json=json.dumps(
                    {
                        "manifest_revision": device.manifest_revision,
                        "session_id": session_id,
                    },
                    sort_keys=True,
                ),
            )
        )
        if self._directory_projector is not None:
            await self._directory_projector.execute(device.identity.device_id)
        return persisted
