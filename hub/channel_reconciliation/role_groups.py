"""Owner-authorized role-group preparation; media and adjudication stay in Channel/Agent."""
from .endpoint_preparation import prepare_owned_endpoints

DEVICE_CONVERSATION_SCOPE = "device.conversation.control"


class RoleGroups:
    def __init__(self, devices, provider, *, channel_binding):
        self._devices, self._provider, self._channel_binding = devices, provider, channel_binding

    async def open(self, selection, *, context):
        context.require_scope(DEVICE_CONVERSATION_SCOPE)
        await prepare_owned_endpoints(selection.devices, context=context, devices=self._devices,
                                      channel_binding=self._channel_binding)
        return await self._provider.role_group(action="open", owner_id=str(context.business_owner_id),
            selection=selection, session_id=selection.session_id)

    async def inspect(self, session_id, *, context, close=False):
        context.require_scope(DEVICE_CONVERSATION_SCOPE)
        return await self._provider.role_group(action="close" if close else "status",
            owner_id=str(context.business_owner_id), session_id=session_id)
