# Eidolon Hub 稳定性推进策略

## 测试驱动闭环

每个架构切片都遵循：Characterize → 首先失败的测试 → 最小实现 → Unit/Contract → Component/Functional → 黑盒 E2E → 架构反思 → 删除死代码 → 全量回归。

测试报告必须区分：

- 纯单元测试与 Fake Repository；
- 进程内组件测试；
- 使用真实 SQLite/HTTP server 的功能测试；
- 依赖外部 PostgreSQL/MQTT/DNS 基础设施的环境测试；
- 从发布 API 之外观察系统的黑盒契约 E2E。

未提供真实基础设施时只能报告 Not Run/Blocked，不能用 Mock 结果替代 Cloud 通过结论。

## 必须稳定的测试面

| 测试面 | 证明内容 |
|---|---|
| Unit | 领域状态机、注册/审批/撤销、连接 lease、Provider reconcile、命令、幂等和失败恢复 |
| Contract | 全部 JSON Schema、生成模型可复现、Provider/Connector conformance、非法边界 |
| Local Functional | SQLite + memory projection + HTTPS/mDNS 配置路径 + Provider HTTP 契约 |
| Cloud Functional | PostgreSQL + MQTT5 + 多实例 claim/fencing + Provider HTTP 契约 |
| Deployment Mode | 同一 artifact、同一 Wire Contract，仅通过配置切换 SQLite/PostgreSQL 和 Connector；Local/Cloud 不自动桥接 |
| Contract E2E | 独立 Reference Device/Provider 只使用发布契约完成注册、审批、grant、active、command、ack/result |
| Failure/Security | 重复/乱序、重启、lease 过期、Provider/DB/Broker 故障、重放、越权、secret 日志泄漏 |

“Local/Cloud 无缝切换”只表示代码和契约无分叉、配置选择 Adapter。它不表示 SQLite 数据自动搬迁到 PostgreSQL，也不表示活跃 Connection/Channel lease 跨部署迁移；设备需要关闭旧 lease 并在目标 Hub 重新连接。

## 当前发布阻塞项

- 本机 PostgreSQL 18/MQTT5 基础路径与 PostgreSQL 并发 claim 已执行；生产 TLS/ACL、restart/network partition/rolling restart 尚未执行；
- Device/Connection fact 与 durable event append 尚未形成同一数据库 Unit of Work，缺少中途进程终止 fault test；
- 兄弟项目尚未实现新 Provider/Device Management 契约，因此不能宣称 Eidolon OS 整栈 E2E。

## 报告规范

每份报告记录提交 SHA、工作树状态、配置模式、环境前提、精确命令、测试数量、耗时、失败与重试、覆盖范围、未执行项和由测试推动的架构修改。报告路径位于 `docs/testing/reports/`，架构发现汇总到 `docs/testing/architecture-findings.md`。
