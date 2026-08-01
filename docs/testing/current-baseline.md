# 重构测试基线（2026-08-01）

- Git 基线：`37f38cdc69c7f4a37000fc9b18fef48601203857`
- Python 测试：134 passed
- 收集分类：Unit 83、Contract 21、Component 11、Functional 6、Architecture 13
- 全 Hub branch coverage：69%
- 新 Domain/Application 主要路径接近 100%，生成模型与若干基础设施/Router 路径显著拉低全局覆盖率。

这是实现前基线的证据说明：当时 PostgreSQL 只验证 factory selection，MQTT 直接调用 handler/codec，mDNS 使用 monkeypatch，Provider 使用 Fake。实现后证据见下方快照及逐类报告。

首先锁定的新不变量：

1. 未审批设备不会触发 Provider；
2. Provider 故障不破坏设备注册事实；
3. Provider Binding 原样中继且不持久化；
4. Provider lifecycle `active` 之前，Channel 不承载命令或上行数据；
5. 断连/撤销由 desired-state reconcile 收敛，不由 Hub 解释 Provider 实现。

## 本轮实现后快照

- Full regression：注入本机 PostgreSQL 18/Mosquitto 后 `158 passed in 10.77s`，无 skip。
- 分类：Unit 92、Contract 25、Component 14、Functional 11、Architecture 15、E2E 1。
- Domain/Application branch coverage：95%；全 Hub branch coverage：78%。
- 详细证据：[reports/README.md](reports/README.md)。
