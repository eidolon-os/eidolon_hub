# Contract E2E Report

- 命令：`uv run pytest -q tests/e2e`
- 结果：1 passed in 0.99s

黑盒链路使用生产 Composition、SQLite、memory-hot Directory 和 TCP Reference Provider，完成真实 P-256 Session proof、注册、管理审批、主动 Acquire、opaque binding、中继 lifecycle、Command/Ack/Result、Directory 查询和 Hub 重启后恢复。

该测试不修改或启动 `eidolon_channel`，不证明真实 WSS/MQTT/LiveKit、音视频或生产部署已经兼容。
