from __future__ import annotations

from hub.api.routers.admin.schemas import AdminDevice
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.device_manager import DeviceManager


async def build_admin_devices(
    runtime: LiveKitAdminRuntime,
    device_manager: DeviceManager,
) -> list[AdminDevice]:
    presence = {item.device_id: item for item in await runtime.get_presence_snapshot()}
    rows: list[AdminDevice] = []

    for device in device_manager.list_all():
        p = presence.get(device.device_id)
        last_seen = device.last_seen
        if p and p.last_seen_at and p.last_seen_at > last_seen:
            last_seen = p.last_seen_at
        rows.append(
            AdminDevice(
                device_id=device.device_id,
                name=device.name,
                kind=device.kind,
                enabled=device.enabled,
                paired=device.paired,
                approved=device.approved,
                approved_at=device.approved_at,
                last_seen=last_seen,
                status=p.status if p else "offline",
                room_name=p.room_name if p else "",
                participant_sid=p.participant_sid if p else "",
                missed_probes=p.missed_probes if p else 0,
            )
        )

    rows.sort(key=lambda item: item.device_id)
    return rows
