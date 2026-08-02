# Hub 测试策略

稳定闭环固定为：

```text
失败测试 → 最小实现 → Unit → Contract → Component/Functional
→ Local/Cloud parity → Black-box E2E → 架构反思 → 全量回归
```

| 层级 | 目标 |
|---|---|
| Architecture | 禁止 Connector/MQTT/NATS/SDK/Data/LiveKit 依赖和跨层基础设施泄漏；禁止持久化 opaque binding |
| Unit | Session/Authority、注册/审批/吊销、Acquire 校验、Channel 状态机、Command、Envelope、安全 Adapter |
| Contract | 全部 JSON Schema、生成 DTO、新 Session/Acquire golden examples、Provider acquisition/lifecycle/data binding |
| Component | SQLite Repository、Directory memory+DB、Composition 生命周期与发布 routes |
| Local Functional | HTTPS Device Access 与 Reference Provider 的 acquire→active→command→ack/result |
| Cloud Functional | 真实 PostgreSQL round-trip 与并发 Hub authority fencing |
| Deployment Mode | Local SQLite/mDNS 与 Cloud PostgreSQL/no-mDNS 发布完全相同 API/Schema，仅 Adapter 配置不同 |
| Contract E2E | 生产 Composition 黑盒完成 P-256 Session、注册/审批、Acquire、Provider Bridge、重启恢复 |

外部 `eidolon_channel`、生产 TLS、DNS/VLAN、网络分区、DB/Provider restart 和 rolling upgrade 不在当前 Hub-only 证据范围内，不得由本地 Reference Provider 测试推导为已通过。
