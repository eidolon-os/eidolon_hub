# Hub 测试策略

稳定闭环固定为：

```text
失败测试 → 最小实现 → Unit → Contract → Component/Functional
→ Local-only runtime → Black-box E2E → 架构反思 → 全量回归
```

| 层级 | 当前目标 |
|---|---|
| Architecture | 禁止 Session/online/data plane 复活；禁止 MQTT/NATS/SDK/Data/LiveKit 和跨层基础设施泄漏；禁止重复 Directory/Channel persistence；禁止绕过原子 Mutation 单写 Device/Event |
| Unit | Enrollment/Token/窗口、三态 policy、幂等投影修复、Handoff/Provision/Revoke、Get/List、JWT、mDNS、SQLite schema/锁、严格配置 |
| Contract | 13 个 JSON Schema、生成模型 freshness、Enrollment golden example、Onboarding 与 Provider Provision/Revoke binding |
| Component | 两表 SQLite Repository、Device+Audit 原子回滚/expected 冲突、启动 Directory 重建、Composition 生命周期和公开 routes |
| Functional | HTTPS Enrollment/Approval/Handoff，以及真实 HTTP Reference Provider Provision/Revoke |
| E2E | 生产 Composition 黑盒完成 Enrollment、审批、opaque Handoff、查询/事件和 Hub 重启恢复 |
| Integration | Kernel 当前真实 consumer 使用独立 exact-Get capability 解析 Hub response，并在 Hub Revocation 后以 CAS 对账 Mount tombstone |

所有新 Domain/Application 分支覆盖率必须不低于 90%，所有状态转移和拒绝路径有明确用例。Integration 证明工作区当前 producer/consumer 契约，不证明真实进程编排。真实小程序、设备、`eidolon_channel`、TLS/DNS/VLAN 和生产故障仍需独立 conformance。

Cloud、PostgreSQL、多实例、Session、online、Command 和设备 data plane 已从产品范围删除，不作为待补测试能力。
