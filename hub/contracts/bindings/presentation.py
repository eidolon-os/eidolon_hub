"""Canonical device output-presentation bindings owned by eidolon_sdk.

What a device is allowed to present is one contract, authored once in the SDK
and read identically by the Hub, the Channel provider and the device. This
binds that vocabulary the way `device.py` binds the device wire contracts, so
the aggregate, its persistence, the Device Control surface and Channel
reconciliation all name the same type by the same path.
"""

from __future__ import annotations

from eidolon_sdk.biz.presentation import DeviceOutputPolicy, OutputSelection
from eidolon_sdk.biz.presentation.device import SetDeviceOutputPolicy

__all__ = [
    "DeviceOutputPolicy",
    "OutputSelection",
    "SetDeviceOutputPolicy",
]
