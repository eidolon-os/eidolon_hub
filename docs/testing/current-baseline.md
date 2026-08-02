# 当前测试基线

- 日期：2026-08-02
- Contract source：`hub/contracts/schemas`
- 测试总数：133
- 默认本地回归：131 passed, 2 PostgreSQL tests skipped（未注入 DSN 时）
- 本机 PostgreSQL 18 回归：133 passed（注入 `postgresql://127.0.0.1:5432/eidolon_hub_test`）

当前锁定的不变量：

1. Discovery 只提供 Descriptor URI；online 只由有效 Device Session 推导。
2. 注册事实不依赖 Provider；Channel 必须由设备主动 Acquire。
3. Hub 不解析或持久化 opaque binding，也不依据 Provider backend 分支。
4. Command/上行 Envelope 同时要求 active Session 与 active Channel。
5. Local/Cloud 只切换 Discovery/Persistence Adapter，不改变 API/Schema，不自动迁移数据。
