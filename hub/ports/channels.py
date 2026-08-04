"""External channel provider and grant-delivery ports."""

from __future__ import annotations

from typing import Protocol

from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ProviderChannelRevocation,
    ProviderDeviceContext,
)


class ChannelProviderContractError(ValueError):
    """The Provider returned a response that violates the control contract."""


class ChannelProviderControl(Protocol):
    async def provision_channels(self, context: ProviderDeviceContext) -> ChannelAssignmentSet: ...
    async def revoke_channels(self, revocation: ProviderChannelRevocation) -> None: ...
