"""Authorize two existing device endpoints without changing their attachments."""
from eidolon_sdk.biz.control.device_conversation import DeviceConversationSelection

from hub.domain.devices.entities import DeviceLifecycleState

from .domain import ChannelProviderError

DEVICE_CONVERSATION_SCOPE = "device.conversation.control"


class DeviceConversations:
    def __init__(self, devices, provider, *, channel_binding):
        self._devices = devices
        self._provider = provider
        self._channel_binding = channel_binding

    async def open(self, selection: DeviceConversationSelection, *, context):
        context.require_scope(DEVICE_CONVERSATION_SCOPE)
        owner = str(context.business_owner_id)
        for ref in selection.devices:
            device = await self._devices.get(ref.device_instance_id)
            if (device is None or device.owner_id != owner
                    or device.owner_domain_id != str(context.owner_domain_id)
                    or device.lifecycle_state != DeviceLifecycleState.APPROVED
                    or device.device_ref != ref):
                raise PermissionError("selected device is outside current Owner lifecycle")
        for ref in selection.devices:
            if not await self._channel_binding.execute(device_ref=ref):
                raise ChannelProviderError("DEVICE_CHANNEL_NOT_READY", retryable=True)
        return await self._provider.device_conversation(action="open", owner_id=owner,
            selection=selection, session_id=selection.session_id)

    async def inspect(self, session_id, *, context, close=False):
        context.require_scope(DEVICE_CONVERSATION_SCOPE)
        return await self._provider.device_conversation(action="close" if close else "status",
            owner_id=str(context.business_owner_id), session_id=session_id)
