"""Device 设备模型."""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class Device(BaseModel):
    """设备模型.

    device_id 由设备生成（UUID 或硬件序列号），Hub 不分配.
    psk_hash = "sha256:" + SHA-256(PSK)，Hub 只存哈希，不存明文 PSK.

    ``approved`` 与 ``paired`` 是两个独立维度:
    - ``approved`` 是操作员意图层 — Hub admin UI 上"批准这台设备进入系统"
    - ``paired``  是协议层 — 设备通过 PSK 完成了握手
    两者交叉成 4 个状态; admin UI 据此渲染不同标签 ("discovered/approved/paired").
    """

    device_id: str
    name: str = ""
    enabled: bool = True
    psk_hash: Optional[str] = None
    paired: bool = False
    # NEW (Phase 25): 操作员批准位. 旧 devices.json 中若 paired=true 但缺这两个字段,
    # DeviceManager.load() 会自动回填 approved=true (见 device_manager.py).
    approved: bool = False
    approved_at: Optional[datetime] = None
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

    def mark_approved(self) -> None:
        """幂等. 已 approved 的设备再调一次不变更 approved_at."""
        if self.approved:
            return
        self.approved = True
        self.approved_at = datetime.now(timezone.utc)

    def to_storage_dict(self) -> dict:
        """序列化供 JSON 持久化使用."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "enabled": self.enabled,
            "paired": self.paired,
            "approved": self.approved,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "psk_hash": self.psk_hash,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_storage_dict(cls, data: dict) -> "Device":
        """从 JSON 数据反序列化.

        向后兼容:
        - 旧记录缺 ``approved``/``approved_at`` -> 在 DeviceManager.load() 里
          按 paired 推导后再实例化, 这里只负责把字符串时间戳还原回 datetime.
        """
        data = dict(data)
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["last_seen"] = datetime.fromisoformat(data["last_seen"])
        if data.get("approved_at"):
            data["approved_at"] = datetime.fromisoformat(data["approved_at"])
        return cls(**data)
