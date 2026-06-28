from __future__ import annotations

from typing import Any

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


async def refresh_admin_devices(
    runtime: LiveKitAdminRuntime,
    device_manager: DeviceManager,
    *,
    control_bridge: Any | None = None,
    command_timeout_seconds: int | None = None,
) -> list[AdminDevice]:
    """Refresh runtime overlays, then return the composed Hub device view.

    Device identity and operator facts remain registry-backed; this only forces
    a fresh LiveKit presence probe so callers do not have to wait for the next
    background interval when dogfooding connectivity.
    """
    device_ids = [device.device_id for device in device_manager.list_all()]
    await runtime.run_probe_cycle(device_ids)
    presence = await runtime.get_presence_snapshot()
    if control_bridge is not None:
        await control_bridge.sync_rooms(
            [
                item.room_name
                for item in presence
                if item.room_name and item.status != "offline"
            ]
        )
    if command_timeout_seconds is not None:
        await runtime.mark_command_timeout(command_timeout_seconds)
    return await build_admin_devices(runtime=runtime, device_manager=device_manager)
