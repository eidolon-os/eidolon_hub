# Component Test Report

- 日期：2026-08-01
- 状态：Passed
- 命令：`uv run pytest -q tests/component`
- 结果：`14 passed in 1.08s`

使用真实 SQLAlchemy asyncio + 临时 SQLite，验证 Hub-owned Device、Command、Challenge、Connection/Authority、Directory、Channel Assignment/Sync/Cursor 和 durable Event Bus；同时验证 Composition 发布的路由和 Directory DB-first cache 行为。

测试发现真实 SQL sync repository 曾把“相同 desired revision 已成功”直接判为不可 claim，与 Unit Fake 的 pending Grant 补投语义不一致。修复后 Repository 只拒绝未过期的 in-flight claim；Application 决定是否已经收敛。另一个失败暴露 EventLedger 删除后遗留的旧 `subject_id` 写入，已从实现删除。Directory 测试还证明 Connection 自然过期会被周期投影为离线，且没有语义变化时 revision 不递增。

限制：SQLite 不证明 PostgreSQL 行锁在真实多进程/网络故障下的全部行为。
