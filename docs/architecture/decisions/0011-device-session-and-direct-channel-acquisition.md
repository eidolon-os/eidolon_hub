# ADR 0011: HTTPS Device Session 与直接 Channel Acquisition

- 状态：Accepted
- 日期：2026-08-02

## 背景

代码核查显示，旧 `ConnectionConnector` Port 只抽象了 mDNS advertiser 与 Hub MQTT adapter，二者不是同一种能力：mDNS 是入口发现，MQTT 实际上是设备建立连接后的通信后端。多 Connector 优先级、signaling ref、内存 mailbox 和后台 desired-state reconciler 因而增加了错误的耦合与 Cloud 多实例一致性问题。

WAN 设备在 Commissioning 时可以直接保存稳定 HTTPS Descriptor URI，不需要 MQTT 才能发现或注册。MQTT 的长连接、QoS 和离线消息能力若有价值，应由外部 Channel Provider 作为 reliable-data backend 提供。

## 决策

1. 删除 Hub MQTT、transport Connector/Supervisor、signaling mailbox、Provider sync worker/SQL claim，以及 device-side Explicit URI/Unicast DNS resolver adapter。
2. mDNS 是可选 Discovery advertiser，只发布与 WAN Commissioning 相同的 HTTPS Descriptor URI。
3. 设备通过 HTTPS Challenge/Proof 建立 `DeviceSessionLease`。Session/Authority 持久化，并且是 Hub 在线状态与 Device Bus 可达性的权威依据。
4. 注册与 Provider 完全分离。设备注册并获批后，用当前 Session 主动调用 `/channels/acquire`。
5. Hub 把 typed device context 调用到唯一配置的 Provider Contract 地址；Provider 返回通用 Assignment 与 opaque binding，Hub 在同一 HTTP 响应中直接中继。
6. Hub 只持久化 Channel metadata；opaque binding 不持久化、不记录、不进入管理 API。
7. Channel lifecycle active 只表示该通信通道可传数据，不参与设备在线判定。命令和上行 Envelope 同时要求 active Session 与 active Channel。

## 契约与实现分层

- Contract：JSON Schema、严格 Wire DTO、固定 HTTPS paths。
- Domain：Session/Authority、Provider-neutral Assignment/Lease/Lifecycle。
- Application：Enroll/Register/Renew/Close/Acquire/Command/Envelope Use Cases。
- Adapter：Zeroconf、Provider HTTP client、SQLite/PostgreSQL Repository、HTTP DataEnvelope Bridge。
- Composition：唯一组装点，根据配置选择 Discovery 和 Persistence Adapter。

## 影响

Provider 故障只使 Acquire 显式失败，不回滚注册事实；设备按 request/operation ID 重试。Hub 不再负责 Provider 资源的后台创建、续期或回收策略，因此 Provider 必须使用有限期 credential/lease，并自行处理过期资源。Hub 对 revoked/expired Session 的命令与上行数据立即拒绝。

Local/Cloud 发布完全相同的 API 和 Schema。Cloud 多实例依靠 PostgreSQL authority fencing、幂等 ID、持久 cursor/event log，不依赖进程内 mailbox、NATS 或 MQTT bootstrap。
