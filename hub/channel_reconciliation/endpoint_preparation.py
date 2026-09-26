"""Shared authorization and channel reconciliation for explicitly selected endpoints."""
from hub.domain.devices.entities import DeviceLifecycleState
from .domain import ChannelProviderError


async def prepare_owned_endpoints(refs, *, context, devices, channel_binding):
    """Validate every lifecycle before refreshing any disposable channel grant."""
    owner = str(context.business_owner_id)
    for ref in refs:
        device = await devices.get(ref.device_instance_id)
        if (device is None or device.owner_id != owner
                or device.owner_domain_id != str(context.owner_domain_id)
                or device.lifecycle_state != DeviceLifecycleState.APPROVED
                or device.device_ref != ref):
            raise PermissionError("selected device is outside current Owner lifecycle")
    for ref in refs:
        if not await channel_binding.execute(device_ref=ref):
            raise ChannelProviderError("DEVICE_CHANNEL_NOT_READY", retryable=True)
