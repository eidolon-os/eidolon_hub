"""Device 设备模型."""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class Device(BaseModel):
    """设备模型.

    device_id 由设备生成（UUID 或硬件序列号），Hub 不分配.
    psk_hash = "sha256:" + SHA-256(PSK)，Hub 只存哈希，不存明文 PSK.
    """

    device_id: str
    name: str = ""
    enabled: bool = True
    psk_hash: Optional[str] = None
    paired: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = Field(default_factory=dict)

    def touch(self) -> None:
        """更新最后可见时间."""
        self.last_seen = datetime.now(timezone.utc)

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def mark_paired(self, psk_hash: str) -> None:
        self.paired = True
        self.psk_hash = psk_hash

    def to_storage_dict(self) -> dict:
        """序列化供 JSON 持久化使用."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "enabled": self.enabled,
            "paired": self.paired,
            "psk_hash": self.psk_hash,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_storage_dict(cls, data: dict) -> "Device":
        """从 JSON 数据反序列化."""
        data = dict(data)
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["last_seen"] = datetime.fromisoformat(data["last_seen"])
        return cls(**data)
