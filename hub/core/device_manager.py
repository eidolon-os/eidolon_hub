"""DeviceManager — 设备注册表，JSON 文件持久化，支持启用/禁用."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from hub.core.device import Device

logger = logging.getLogger(__name__)


class DeviceManager:
    """管理已配对设备的注册表.

    所有设备存储在 data/devices.json 中，Hub 启动时加载，运行期间在内存操作，
    定期或显式调用 save() 持久化到磁盘.
    """

    def __init__(self, storage_path: Path | str = "data/devices.json"):
        self._storage_path = Path(storage_path)
        self._devices: dict[str, Device] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """从磁盘加载设备列表.

        Phase 25 迁移: 旧 ``devices.json`` 不含 ``approved`` 字段. 我们用"已配对
        意味着曾被操作员认可"这一启发式自动回填 ``approved=true`` (并把
        ``approved_at`` 用 ``created_at`` 作占位), 避免历史设备升级后显示成
        "已配对但未批准"这种自相矛盾的状态. 全新装机的设备 paired/approved 都是
        False, 必须走 admin 显式审批才能 approved=True.
        """
        if not self._storage_path.exists():
            self._devices = {}
            return
        async with self._lock:
            try:
                with open(self._storage_path) as f:
                    data = json.load(f)
                devices_list = data.get("devices", {})
                migrated = 0
                for k, v in devices_list.items():
                    if v.get("paired") and "approved" not in v:
                        v["approved"] = True
                        v["approved_at"] = v.get("created_at")
                        migrated += 1
                self._devices = {
                    k: Device.from_storage_dict(v) for k, v in devices_list.items()
                }
                logger.info("Loaded %d devices from %s", len(self._devices), self._storage_path)
                if migrated:
                    logger.info("Phase 25 migration: auto-approved %d legacy paired devices", migrated)
                    # 立即落盘补回 approved 字段, 防止下次重启再触发同样迁移.
                    await self._save_unlocked()
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to load devices file: %s, starting fresh", e)
                self._devices = {}

    async def save(self) -> None:
        """持久化设备列表到磁盘 (公开入口, 拿锁)."""
        async with self._lock:
            await self._save_unlocked()

    async def _save_unlocked(self) -> None:
        """实际落盘逻辑. 调用方必须已持有 ``self._lock``.

        独立出来是为了让 load() 的迁移分支能在自己已经持锁的情况下复用同样的
        原子写流程, 而不至于在 ``async with self._lock`` 内部再调 save() 死锁.
        """
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "devices": {k: v.to_storage_dict() for k, v in self._devices.items()},
        }
        tmp_path = self._storage_path.with_suffix(".json.tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(self._storage_path)
        logger.debug("Saved %d devices to %s", len(self._devices), self._storage_path)

    def register(self, device_id: str, name: str = "", psk_hash: Optional[str] = None) -> Device:
        """注册新设备或更新已存在设备."""
        if device_id in self._devices:
            device = self._devices[device_id]
            if name:
                device.name = name
            if psk_hash:
                device.mark_paired(psk_hash)
            device.touch()
        else:
            device = Device(
                device_id=device_id,
                name=name,
                psk_hash=psk_hash,
                paired=psk_hash is not None,
            )
            self._devices[device_id] = device
            logger.info("Registered new device: %s (%s)", device_id, name or "unnamed")
        return device

    def get(self, device_id: str) -> Optional[Device]:
        """根据 device_id 查询设备."""
        return self._devices.get(device_id)

    def get_or_raise(self, device_id: str) -> Device:
        """查询设备，不存在则抛出异常."""
        device = self.get(device_id)
        if device is None:
            raise KeyError(f"Device not found: {device_id}")
        return device

    def enable(self, device_id: str) -> Device:
        """启用设备."""
        device = self.get_or_raise(device_id)
        device.enable()
        logger.info("Enabled device: %s", device_id)
        return device

    def disable(self, device_id: str) -> Device:
        """禁用设备（不断开现有连接，由调用方处理）."""
        device = self.get_or_raise(device_id)
        device.disable()
        logger.info("Disabled device: %s", device_id)
        return device

    async def approve(self, device_id: str) -> Device:
        """操作员批准设备. 幂等, 已批准则 no-op (保留首次 approved_at).

        async 是因为完成内存改动后会自动落盘 ``devices.json`` —— admin 编排链路
        要求"批准即生效, 不依赖外部周期 save()". 调用方拿到返回值即可视为已持久化.
        """
        async with self._lock:
            device = self.get_or_raise(device_id)
            already = device.approved
            device.mark_approved()
            await self._save_unlocked()
        if not already:
            logger.info("Approved device: %s", device_id)
        return device

    def list_all(self) -> list[Device]:
        """返回所有设备列表."""
        return list(self._devices.values())

    def list_enabled(self) -> list[Device]:
        """返回所有已启用设备列表."""
        return [d for d in self._devices.values() if d.enabled]

    def list_paired(self) -> list[Device]:
        """返回所有已配对设备列表."""
        return [d for d in self._devices.values() if d.paired]

    def remove(self, device_id: str) -> None:
        """删除设备."""
        if device_id in self._devices:
            del self._devices[device_id]
            logger.info("Removed device: %s", device_id)

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._devices
