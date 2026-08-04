"""Stable device identity values, independent of transport addresses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("device_id is required")
