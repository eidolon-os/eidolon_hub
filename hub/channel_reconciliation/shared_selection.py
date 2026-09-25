"""Resolve an authenticated Owner's selected devices from the existing projection.

No bindings, channel credentials or operation IDs are accepted from a client.
This service prepares transport specifications; it grants no media/Agent access.
"""

import json

from eidolon_sdk.biz.control.shared_session import SharedSessionSelection

from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.identity import ManagementPrincipal

from .ports import ChannelDeviceProjectionReader
from .specification import channel_device_payload


class ResolveSharedSelection:
    def __init__(self, devices: ChannelDeviceProjectionReader):
        self._devices = devices

    async def execute(
        self, selection: SharedSessionSelection, *, principal: ManagementPrincipal
    ) -> dict:
        # Principal must be produced by the existing boundary authenticator.
        # An unscoped service reader is not an Owner acting on its devices.
        if principal.owner_id is None:
            raise PermissionError("an authenticated Owner scope is required")
        devices = []
        for ref in selection.devices:
            device = await self._devices.get(ref.device_instance_id)
            if (
                device is None
                or device.owner_id != principal.owner_id
                or device.lifecycle_state != DeviceLifecycleState.APPROVED
                or device.device_ref != ref
            ):
                raise PermissionError("selected device is outside current Owner lifecycle")
            devices.append(device)
        specifications = []
        for device in devices:
            manifest = json.loads(device.manifest_json)
            if not isinstance(manifest, dict):
                raise ValueError("accepted Manifest must be an object")
            specifications.append(
                {
                    "device_ref": device.device_ref.model_dump(mode="json"),
                    "device": channel_device_payload(
                        owner_id=device.owner_id,
                        display_name=device.display_name,
                        manifest_id=device.manifest_id,
                        manifest=manifest,
                        manifest_revision=device.manifest_digest,
                        output_policy=device.output_policy,
                    ),
                }
            )
        return {
            "selection": selection.model_dump(mode="json"),
            "owner_id": principal.owner_id,
            "specifications": specifications,
        }
