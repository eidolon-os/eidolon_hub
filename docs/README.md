# Eidolon Hub 文档导航

本目录按“当前规范 → 代码实现 → 对外集成 → 决策历史 → 测试证据”的顺序组织。除特别标注外，代码、JSON Schema 和自动化测试是最终事实源；已被取代的 ADR 只解释决策演进，不描述当前运行时。

## 推荐阅读路径

1. [项目 README](../README.md)：用几分钟理解 Hub 的角色、边界、流程、API 和运行方法。
2. [Device Control Subsystem 架构标尺](architecture/device-control-subsystem.md)：判断一段需求或代码是否应该属于 Hub 的规范。
3. [代码架构与完整代码导览](code-architecture.md)：逐目录、逐文件理解 `hub/` 的实现。
4. [Onboarding 控制面架构](architecture/hub-three-plane.md)：理解关键流程、状态、持久化和 Provider 边界。
5. [Eidolon OS 集成边界](architecture/eidolon-os-integration.md)：理解设备、Provider、管理客户端和 metadata consumer 如何使用 Hub。
6. [测试策略](testing/test-strategy.md) 与 [测试报告索引](testing/reports/README.md)：理解哪些结论已有自动化证据，哪些仍待真实项目集成验证。

## 当前有效设计

| 文档 | 性质 | 回答的问题 |
|---|---|---|
| [Device Control Subsystem 架构标尺](architecture/device-control-subsystem.md) | Normative | Hub 在 Eidolon OS 中是什么、什么代码应该进入 Hub |
| [代码架构与完整代码导览](code-architecture.md) | Current | 每个目录和代码文件做什么、运行时如何组装 |
| [Onboarding 控制面架构](architecture/hub-three-plane.md) | Current | Enrollment、Approval、Handoff、存储和内存投影如何协作 |
| [Eidolon OS 集成边界](architecture/eidolon-os-integration.md) | Current | 哪些项目是契约使用方、当前验证到了哪里 |
| [当前完整架构 Review](architecture/eidolon-hub-current-architecture-review.md) | Evidence snapshot | 当前实现、数据库、配置、测试基线和未完成生产门槛 |

`hub-three-plane.md` 的文件名来自早期演进，当前没有 Connection Plane 或三套运行时。

## 架构决策记录

ADR 永远保留原始背景，但只有 Accepted 且未被后续 ADR 修订的内容可以直接作为当前设计。当前角色和运行时基线以 ADR 0018 为准。

| ADR | 当前状态 | 主题 |
|---|---|---|
| [0001](architecture/decisions/0001-three-plane-boundary.md) | Superseded | 早期 Connection/Management/Channel 三平面拆分 |
| [0002](architecture/decisions/0002-sdk-and-data-ownership.md) | Accepted | Hub 自有契约和运行时数据，不依赖 SDK/DataStore |
| [0003](architecture/decisions/0003-opaque-channel-binding.md) | Revised by 0017 | Provider binding 保持 opaque；Profile/Grant 持久化已删除 |
| [0004](architecture/decisions/0004-routed-discovery.md) | Revised by 0011 | mDNS 不跨子网；当前 Hub 基线是 Commissioning URI |
| [0005](architecture/decisions/0005-standard-data-envelope-bridge.md) | Superseded by 0013 | 已删除 Hub DataEnvelope Bridge |
| [0006](architecture/decisions/0006-device-authority-fencing.md) | Superseded by 0015 | 已删除 Cloud authority/fencing |
| [0007](architecture/decisions/0007-characterization-quarantine.md) | Completed history | 旧实现隔离与删除过程 |
| [0008](architecture/decisions/0008-hub-device-bus-without-nats.md) | Superseded by 0013 | 去 NATS 结论保留，Device Bus 定位已删除 |
| [0009](architecture/decisions/0009-database-authority-memory-projection.md) | Revised by 0015/0017 | SQLite 权威 + 内存热目录保留，Channel metadata 已删除 |
| [0010](architecture/decisions/0010-channel-provider-contract-boundary.md) | Superseded | 旧 desired-state/reconciler Provider 方案 |
| [0011](architecture/decisions/0011-device-session-and-direct-channel-acquisition.md) | Superseded | 旧独立 Acquire/Data Bridge/Cloud 方案 |
| [0012](architecture/decisions/0012-deployment-owned-api-and-strict-profiles.md) | Accepted, revised | 部署层拥有监听/TLS，Hub 使用严格单一配置 |
| [0013](architecture/decisions/0013-hub-is-device-manager-not-device-bus.md) | Revised by 0018 | Hub 不是 Device Bus；长期 Device Manager 定位已收敛 |
| [0014](architecture/decisions/0014-defer-third-party-observability.md) | Accepted | 暂缓 Hub 内置第三方 telemetry |
| [0015](architecture/decisions/0015-local-only-single-process-sqlite.md) | Accepted, revised | Local-only、单进程、独占 SQLite 保留 |
| [0016](architecture/decisions/0016-device-state-storage-and-query-contract.md) | Revised by 0018 | Get/List 保留；状态名和 online 已修订 |
| [0017](architecture/decisions/0017-channel-assignment-is-request-scoped-relay.md) | Revised by 0018 | Assignment 请求级透传保留，改由 Handoff 交付 |
| [0018](architecture/decisions/0018-hub-is-onboarding-and-provider-handoff.md) | Accepted | Hub 只负责 Onboarding、Registry、Policy、Directory 与 Provider Handoff |

## 测试与工程证据

| 入口 | 内容 |
|---|---|
| [测试策略](testing/test-strategy.md) | 测试分层、场景、门禁和边界 |
| [当前测试基线](testing/current-baseline.md) | 最近一次可复现的命令与结果 |
| [测试报告索引](testing/reports/README.md) | Architecture、Unit、Contract、Component、Functional、E2E 报告 |
| [架构发现](testing/architecture-findings.md) | 测试推动重构时发现的边界和复杂度问题 |
| [代码审计记录](testing/code-inventory.md) | 逐文件有效性审计的历史结论与当前导览入口 |
| [测试矩阵](refactor/test-matrix.md) | 需求、测试层级和验收结果的映射 |

## 重构记录

- [Hub 重构日志](refactor/hub-refactor-log.md)：按阶段记录行为、失败测试、代码/配置变化、结果和反思。
- [测试矩阵](refactor/test-matrix.md)：记录各阶段必须通过的测试组合。

这些文件是工程过程记录，不应覆盖当前规范。如果记录与当前代码或架构标尺冲突，以当前 Schema、测试和 Normative 文档为准。
