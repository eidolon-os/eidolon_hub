# 2026-08-01 稳定性验收报告索引

共同基线：`37f38cdc69c7f4a37000fc9b18fef48601203857`。报告生成时为未提交工作树，结果对应本轮 Channel Provider desired-state 重构；不得把这些结果归属于基线提交。

| 测试面 | 状态 | 报告 |
|---|---|---|
| Unit | Passed | [unit-test-report.md](unit-test-report.md) |
| Contract | Passed | [contract-test-report.md](contract-test-report.md) |
| Component/SQLite | Passed | [component-test-report.md](component-test-report.md) |
| Local Functional | Passed | [local-functional-report.md](local-functional-report.md) |
| Cloud Infrastructure | Passed for local PostgreSQL 18/Mosquitto scope | [cloud-functional-report.md](cloud-functional-report.md) |
| Deployment Mode | Passed | [deployment-mode-report.md](deployment-mode-report.md) |
| Contract E2E | Passed | [contract-e2e-report.md](contract-e2e-report.md) |
| Architecture/Dependencies | Passed | [architecture-test-report.md](architecture-test-report.md) |
| Failure/Security | Passed for automated layers; deployment faults blocked | [failure-security-report.md](failure-security-report.md) |
| Full Regression | Passed, no skips with local infrastructure | [full-regression-report.md](full-regression-report.md) |

Passed 只证明报告中列出的证据层级。本机 PostgreSQL/MQTT 基础连接通过不等价于生产 TLS/ACL、Broker/DB restart、VLAN、DNS 或滚动部署验证。
