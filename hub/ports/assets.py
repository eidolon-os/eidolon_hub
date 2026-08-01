"""Owner/device asset port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True, slots=True)
class DeviceAsset:
    asset_id: str
    owner_id: str
    content_type: str
    sha256: str
    size_bytes: int


class DeviceAssetRepository(Protocol):
    async def describe(self, asset_id: str) -> DeviceAsset | None: ...
    def open(self, asset_id: str) -> AsyncIterator[bytes]: ...
