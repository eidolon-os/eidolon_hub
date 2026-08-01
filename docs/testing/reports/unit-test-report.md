# Unit Test Report

- 日期：2026-08-01
- 状态：Passed
- 命令：`uv run pytest -q tests/unit`
- 结果：`92 passed in 0.55s`
- Domain/Application branch coverage 命令：`uv run pytest -q tests/unit --cov=hub.domain --cov=hub.application --cov-branch --cov-report=term-missing`
- 覆盖结果：`972 statements, 268 branches, 95% total`

覆盖内容包括设备注册/审批/撤销、Connection registry 与 fencing、Command 状态机和 TTL、Channel desired-state、Grant 补投/轮换、lifecycle 门禁、DataEnvelope 去重与安全校验、JWT、P-256、发现 Adapter、内存 Directory、telemetry，以及生产 Clock/安全 ID Adapter。

本轮首先失败后修复的用例：临近过期的 active Assignment 必须产生新 operation generation；未确认的 pending Grant 在仍可用时复用 operation，在刷新窗口内轮换；Provider 的 Grant 必须覆盖 30 秒刷新窗口；Directory Projection Worker 必须立即运行并可干净停止。

限制：Repository、Clock、Provider 和 Connector 多为 Fake。真实事务互斥由 Component/Cloud 报告单独证明。
