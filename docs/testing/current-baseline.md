# 当前测试基线

- 日期：2026-08-04
- Contract source：`hub/contracts/schemas`
- 测试总数：121
- Local-only 全量回归：`121 passed in 4.28s`，无 skip
- Domain + Application branch coverage：97%（432 statements、114 branches）

分层结果：Architecture 25、Unit 67、Contract 15、Component 9、Functional 4、E2E 1。

当前锁定的不变量：

1. Hub 只处理 Descriptor、Enrollment、审批/吊销和 Provider Handoff，不维护长期设备连接。
2. lifecycle 只有 `pending-approval / approved / revoked`；approved 不表示 online。
3. Handoff 必须通过高熵 retrieval token、未过期窗口和 approved policy 三重校验。
4. Hub 只保存 Token hash，明文与 opaque binding 都不进入 Directory、事件或数据库。
5. Provider 交接后拥有所有连接、Heartbeat/Lease、online 和设备业务 Data/Media。
6. SQLite 只有 `hub_devices`、`hub_events`；Directory 从设备事实启动重建并内存热读。
7. Hub 只有 Local 单进程形态；旧 Schema 直接拒绝，无 migration/兼容。
8. OS 项目通过 Owner-scoped Get/List/Event cursor 获取 metadata，不读取 Hub 内部存储。
9. Provider Provision/Revoke 使用稳定 operation ID 幂等；Hub 不按 backend 分支。
10. 同一 SQLite 由文件锁排他打开，第二进程 fail closed。

静态门禁：Contract generation check、Ruff、Import Linter 和 `uv lock --check` 全部通过。Clean wheel 为 96 files，只含当前 Onboarding/Management runtime 和单一 `settings.yaml`。
