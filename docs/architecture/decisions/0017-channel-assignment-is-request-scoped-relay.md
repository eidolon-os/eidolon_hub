# ADR 0017：Channel Assignment 是请求级透传，不是 Hub 状态

- 状态：Accepted；Assignment 改由独立 Handoff 请求透传，详见 ADR 0018
- 日期：2026-08-03
- 补充：ADR 0013、0015、0016
- 取代：ADR 0010/0011 中的 Channel metadata persistence 与 Lifecycle callback

## 背景

> Assignment 请求级透传和不持久化仍有效；当前不再通过认证 Register retry，而通过有界 Enrollment Handoff 交付，详见 [ADR 0018](0018-hub-is-onboarding-and-provider-handoff.md)。

代码审计证明，Hub 保存的 Channel Assignment metadata 只有写入路径：Provision 后写 SQLite，
Provider Lifecycle callback 再修改状态。Device Directory、在线判定、管理查询、授权和外部事件流
均不读取这些记录；Revoke 又直接按 device ID 调用 Provider，不需要逐条 Channel 记录。

这使 Hub 无收益地复制 Provider 的 Channel 状态机，并额外引入一张表、一个 Repository、一个
Application Use Case、一个 Provider 入站 Router 和一组生命周期契约。

## 决策

1. Hub 只在已审批设备的认证 Register retry 中同步调用 Provider Provision。
2. Hub 校验 operation ID、device ID、manifest revision、Assignment 通用字段和有效期。
3. Assignment 与 opaque binding 都只存在于当前请求调用栈，并在同一注册响应中转交设备。
4. Hub 不持久化 Assignment，不接收 Provider Lifecycle callback，也不查询 Channel active 状态。
5. Device online 只由 Hub 持久 Session Lease 推导。
6. Device revocation 先关闭 Hub Device/Session，再按 device ID 调用 Provider Revoke；Provider 独立
   回收自己创建的 Channel、credential 和媒体资源。
7. Provider 必须按 operation ID 幂等，并为 credential 设置有限有效期；Hub 当前没有 durable outbox，
   因而 Provider 不可达时由相同 management request 重试 Revoke。

## 结果

Hub 的 Channel 边界缩减为一个 outbound Port 和一个 HTTP Adapter。数据库只保存设备管理事实、
认证 Session、Challenge、Directory 投影和管理审计。Provider 可独立实现 MQTT、WSS、LiveKit 或
其他 backend，而不会把其运行状态或故障模型泄漏回 Device Manager。
