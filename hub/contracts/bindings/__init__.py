"""Strict runtime bindings layered over generated Pydantic v2 shapes."""

from hub.contracts.bindings.channel import (
    ChannelAcquisitionRequest,
    ChannelAcquisitionResponse,
    ChannelAssignment,
    ChannelLifecycleEvent,
    DataEnvelope,
    ProviderChannelAcquisitionRequest,
    ProviderChannelAcquisitionResponse,
)
from hub.contracts.bindings.common import DeviceIdentity
from hub.contracts.bindings.device import DeviceManifest, DeviceRegistration
from hub.contracts.bindings.session import (
    HubDescriptor,
    SessionAccepted,
    SessionChallenge,
    SessionClosed,
    SessionHeartbeat,
    SessionHello,
    SessionProof,
    SessionRegistration,
)

__all__ = [
    "ChannelAcquisitionRequest",
    "ChannelAcquisitionResponse",
    "ChannelAssignment",
    "ChannelLifecycleEvent",
    "DataEnvelope",
    "DeviceIdentity",
    "DeviceManifest",
    "DeviceRegistration",
    "HubDescriptor",
    "ProviderChannelAcquisitionRequest",
    "ProviderChannelAcquisitionResponse",
    "SessionAccepted",
    "SessionChallenge",
    "SessionClosed",
    "SessionHeartbeat",
    "SessionHello",
    "SessionProof",
    "SessionRegistration",
]
