# Component Test Report

- 日期：2026-08-04
- 命令：`uv run pytest -q tests/component`
- 结果：`12 passed in 4.18s`

使用真实 SQLAlchemy asyncio 与临时 SQLite，验证 `hub_devices`、`hub_events`、Enrollment ID 查询、管理事件流、Device+Audit 同事务回滚、expected snapshot 并发冲突、event ID 复用时 Device 回滚、从 Device 事实启动重建内存 Directory，以及生产 Composition routes。组件门禁断言旧 Device Access、Session、Command 和 Provider ingress 路径不存在。
