"""Stable device identity values, independent of transport addresses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    public_key_fingerprint: str
    tenant_id: str = "local"

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("device_id is required")
        if not self.public_key_fingerprint.strip():
            raise ValueError("public_key_fingerprint is required")
        if not self.tenant_id.strip():
            raise ValueError("tenant_id is required")
