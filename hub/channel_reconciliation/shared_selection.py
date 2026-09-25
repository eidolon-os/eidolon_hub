"""Resolve an authenticated Owner's selected devices from the existing projection.

No bindings, channel credentials or operation IDs are accepted from a client.
This service prepares transport specifications; it grants no media/Agent access.
"""

import json

from eidolon_sdk.biz.control.shared_session import SharedSessionSelection

from hub.admission.domain import ActorContext
from hub.domain.devices.entities import DeviceLifecycleState

from .application import ReconcileChannelBinding
from .domain import ChannelProviderError
from .ports import ChannelDeviceProjectionReader
from .specification import channel_device_payload

SHARED_SESSION_SCOPE = "device.shared-session.control"


class SharedDeviceSessions:
    def __init__(self, devices: ChannelDeviceProjectionReader, provider, *,
                 channel_binding: ReconcileChannelBinding):
        self._devices = devices
        self._provider = provider
        self._channel_binding = channel_binding

    async def execute(self, selection: SharedSessionSelection, *, context: ActorContext) -> dict:
        context.require_scope(SHARED_SESSION_SCOPE)
        owner_id = str(context.business_owner_id)
        devices = []
        for ref in selection.devices:
            device = await self._devices.get(ref.device_instance_id)
            if (
                device is None
                or device.owner_id != owner_id
                or device.owner_domain_id != str(context.owner_domain_id)
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
            "owner_id": owner_id,
            "specifications": specifications,
        }

    async def open(self, selection: SharedSessionSelection, *, context: ActorContext) -> dict:
        resolved = await self.execute(selection, context=context)
        # A standing transport can remain connected after its join token expires.
        # Reuse the same lifecycle reconciler as device config pulls; selection
        # authorization above must complete before any binding is changed.
        for ref in selection.devices:
            if not await self._channel_binding.execute(device_ref=ref):
                raise ChannelProviderError("SHARED_CHANNEL_NOT_READY", retryable=True)
        return await self._provider.open_shared_session(
            selection=selection,
            owner_id=resolved["owner_id"],
            specifications=resolved["specifications"],
        )

    async def close(self, session_id: str, *, context: ActorContext) -> dict:
        context.require_scope(SHARED_SESSION_SCOPE)
        # Cleanup must remain possible after a selected device was revoked.
        return await self._provider.close_shared_session(
            session_id=session_id,
            owner_id=str(context.business_owner_id),
        )
