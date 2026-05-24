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
        rows.append(
            AdminDevice(
                device_id=device.device_id,
                name=device.name,
                enabled=device.enabled,
                paired=device.paired,
                approved=device.approved,
                approved_at=device.approved_at,
                last_seen=p.last_seen_at if p else device.last_seen,
                status=p.status if p else "offline",
                room_name=p.room_name if p else "",
                participant_sid=p.participant_sid if p else "",
                missed_probes=p.missed_probes if p else 0,
            )
        )

    for device_id, p in presence.items():
        if device_manager.get(device_id):
            continue
        # Presence-only 设备 (从 LiveKit room 抓到但 device_manager 还没注册):
        # 既未配对也未批准. 出现在列表里只是为了让 operator 能看见 "嘿, 有个
        # 没注册的设备在 room 里, 你得处理一下".
        rows.append(
            AdminDevice(
                device_id=device_id,
                enabled=True,
                paired=False,
                approved=False,
                last_seen=p.last_seen_at,
                status=p.status,
                room_name=p.room_name,
                participant_sid=p.participant_sid,
                missed_probes=p.missed_probes,
            )
        )

    rows.sort(key=lambda item: item.device_id)
    return rows
