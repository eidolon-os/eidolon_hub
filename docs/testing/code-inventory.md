# Hub 代码清单与删除判定（2026-08-01）

## 保留的层次

| 层 | 所有权 | 主要内容 |
|---|---|---|
| Contract | Wire source 与兼容性 | JSON Schema、AsyncAPI、Pydantic binding、examples、generated raw models |
| Domain | 协议无关规则 | Device/Manifest、Connection/Authority、Command、generic Channel lease/lifecycle/DataEnvelope values |
| Application | 用例与一致性 | 注册/审批/撤销、Directory projection、Command ledger、Provider desired-state reconcile |
| Ports | 最小替换边界 | Repository、Connector、Provider control、Grant relay、Data transport、Clock/Identity/Event Bus |
| Adapters/Interfaces | 技术实现 | SQLite/PostgreSQL、Directory scheduler、mDNS/DNS/URI、HTTPS/MQTT5、Provider HTTP、JWT/P-256、OpenTelemetry |
| Composition | 唯一组装点 | 配置校验、资源生命周期、Adapter 选择和 Use Case 注入 |

Guard/Sense Schema 与 binding 虽然当前不在生产 Composition 中执行，仍是 Hub wheel 发布且有 contract tests 的公共契约，不能按“运行时 import 为零”直接删除。Generated 文件由 Schema 产生，也不能手工精简。

## 本轮删除

删除 Profile selection、Provision/Renew/Revoke orchestration、设备 Channel negotiation、旧 Provisioner client 和对应测试/Schema。它们把 Provider 策略放进 Hub，与新契约边界冲突。

同时删除没有生产 import、没有 Composition 装配且不再拥有业务语义的旧 Admin client、SSE helper、Device Event wrapper、Device Signature/Management Access 旧实现、Asset/Owner Context Ports、Capability 常量、旧 logging helper、空 Messaging package 和 EventLedger/Guard persistence 残余。Event Bus 作为 Hub 对外 durable device event stream 保留。

低覆盖的 MQTT、mDNS、telemetry、HTTP Router 和 Composition 均由生产 Composition/发布 API 使用，属于需要部署测试的基础设施代码，不是死代码。
