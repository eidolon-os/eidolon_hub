# ADR 0013: Hub 是 Device Manager，不是 Device Bus

- 状态：Accepted；长期 Device Manager/Session/online 部分由 ADR 0018 修订
- 日期：2026-08-02
- 取代：ADR 0005、0008 中的 Device Bus/DataEnvelope 结论，以及 ADR 0011 的独立 Acquire 流程

## 背景

> 当前实现以 [ADR 0018](0018-hub-is-onboarding-and-provider-handoff.md) 为准：Hub 仍不是 Device Bus，但也不再维护长期 Session 或 online。

Hub 已经拥有设备身份、Session、Manifest、Owner/审批/吊销和公共目录。若它同时保存 Command Ledger，并在 Provider 与上层之间转换 DataEnvelope，Hub 会重新进入设备 data plane：每个 backend、业务 payload、QoS 和消费方都会扩大 Hub 的耦合与容量责任。

代码核查还显示，Channel Provider 已经是设备实际通信终点。Command/Ack/Result/State/Event 先进入 Provider 再绕回 Hub 没有增加设备管理语义，只增加双跳、Cursor、表、API 和故障状态。

## 决策

1. Hub 只实现 Device Manager 和低频 Channel Control。
2. 删除 Hub Command Domain/Use Case/Repository/表/API、DataEnvelope、Channel Cursor 和 Provider Data Bridge。
3. 所有 Device Data/Audio/Video 均在设备、Channel Provider 与其 consumer 之间流动。
4. Hub 只配置一个 Provider 契约基址；固定调用 Provision/Revoke，不接收 Lifecycle。
5. Channel backend/策略/凭据由 Provider 独占。Hub 只转交 opaque binding，且不持久化。
6. 首次注册未审批时不 provision。审批后设备以认证 Register retry 获取 binding，取消额外 `/channels/acquire`。
7. Device online 只由有效 Session 推导；Channel lifecycle 完全由 Provider 所有。

## 结果

Hub 的吞吐与故障域不再随设备业务消息或媒体增长。Provider 可独立演进 MQTT/WSS/LiveKit；Agent 数据契约也可独立于设备管理契约。代价是 Hub 不再提供命令状态查询或设备状态黑板；需要这些能力的项目必须从 Channel 数据服务获得，而不是要求 Hub 建立兼容桥。

Hub↔Provider 控制调用继续使用 authenticated HTTP JSON。控制面低频、单次 payload 小，gRPC stream 的连接管理、LB、健康检查和生成工具复杂度没有代码证据支撑；Transport 仍可在 Adapter 层替换。

显式 revocation 采用 Hub DB fail-closed 后同步通知 Provider，并允许相同 operation ID 重试。当前没有 durable outbox；Provider 不可达期间，已签发凭据的残余有效性由 Provider lease 上限约束。这是后续故障注入必须验证的生产门禁，不在本 ADR 中臆造额外基础设施。
