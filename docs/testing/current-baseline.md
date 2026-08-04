# 当前测试基线

- 日期：2026-08-04
- Contract source：`hub/contracts/schemas`
- 测试总数：128
- Local-only 全量回归：`128 passed in 11.32s`，无 skip
- Domain + Application branch coverage：97.30%（441 statements、114 branches）

分层结果：Architecture 26、Unit 70、Contract 15、Component 12、Functional 4、E2E 1。

当前锁定的不变量：

1. Hub 只处理 Descriptor、Enrollment、审批/吊销和 Provider Handoff，不维护长期设备连接。
2. lifecycle 只有 `pending-approval / approved / revoked`；approved 不表示 online。
3. Handoff 必须通过高熵 retrieval token、未过期窗口和 approved policy 三重校验。
4. Hub 只保存 Token hash，明文与 opaque binding 都不进入 Directory、事件或数据库。
5. Provider 交接后拥有所有连接、Heartbeat/Lease、online 和设备业务 Data/Media。
6. SQLite 只有 `hub_devices`、`hub_events`；Device Fact、request 幂等标记与 Audit 原子提交，Directory 从设备事实启动重建并内存热读。
7. Hub 只有 Local 单进程形态；旧 Schema 直接拒绝，无 migration/兼容。
8. OS 项目通过 Owner-scoped Get/List/Event cursor 获取 metadata，不读取 Hub 内部存储。
9. Provider Provision/Revoke 使用稳定 operation ID 幂等；Hub 不按 backend 分支。
10. 同一 SQLite 由文件锁排他打开，第二进程 fail closed。
11. Hub 只拥有 Device→Owner Admission；Kernel 唯一拥有同一 Owner Namespace 内 Device→Companion Mount，Hub 不保存 `companion_id`。
12. Approval/Revocation 审计主体来自已验证 JWT `sub`，不能由请求体指定；Owner scope 与操作主体保持正交。
13. Enrollment/Approval/Revocation fingerprint 使用 canonical JSON + SHA-256，不依赖可碰撞的字符串分隔符。

静态门禁：Contract generation check、Ruff、Import Linter 和 `uv lock --check` 全部通过。Clean wheel 构建通过，共 97 files，不含 NATS、LiveKit、MQTT、SDK、Data、PostgreSQL 或 OpenTelemetry 运行依赖。
