# ADR 0018：Hub 只负责 Device Onboarding 与 Provider Handoff

- 状态：Accepted
- 日期：2026-08-04
- 修订：ADR 0013、0015、0016、0017 中的长期 Session、online、注册重试和持久 Directory 结论

## 背景

人工审批后，设备的长期连接、认证、心跳、重连、数据和媒体都由 Channel Provider 接管。Hub 原有的 Challenge/Proof、持久 Session、Heartbeat、Lease、online 投影和 Directory 后台重投影没有继续参与设备管理授权，也造成 Hub 与 Provider 重复维护连接生命周期。

人工 Approval 已是设备加入 Eidolon OS 的授权边界。设备在批准前只需安全关联一次 Enrollment，在批准后取得一次 Provider Assignment。因此长期管理 Session 不是当前产品目标所需的最小机制。

## 决策

1. Hub 定位为 Device Onboarding、Registry、Policy Authority 和 metadata Directory，不是长期 Device Manager。
2. 设备 API 只保留 Descriptor、Enrollment 和有界 Handoff。
3. 设备生成高熵 retrieval token；Hub 只存 SHA-256 hash，并用 constant-time compare 验证。
4. retrieval token 只关联原 Enrollment 调用方。小程序必须通过二维码、短码、BLE/SoftAP 或物理确认识别实际设备；人工 Approval 才授予资格并绑定 Owner。
5. 生命周期收敛为 `pending-approval -> approved -> revoked`。`approved` 不代表 online，Hub 不再表达 online。
6. Approval 重新打开有限 Handoff 窗口。Approved Handoff 以稳定 enrollment ID 调用 Provider Provision，并在同一响应中透传请求级 opaque Assignment。
7. Provider 交接后完全拥有长期认证、连接、Heartbeat、Lease、重连、online、Command、State、Event 和 Audio/Video。
8. 删除 Challenge、Proof、Session、Heartbeat、Close、Directory Worker 及相关 Domain、Port、Adapter、API、Schema、配置和测试。
9. SQLite 只保存 `hub_devices` 与 `hub_events`。Directory 直接从设备事实启动重建，不再持久化重复投影表。
10. 不保留开发期旧协议、旧数据库或旧配置兼容。

## 安全边界

未认证 Enrollment 允许恶意方占用 Device ID，因此产品配网流程不能仅按自声明的 Device ID 展示并批准。管理端必须展示并校验来自设备外壳二维码、近场链路或物理动作的 Enrollment 标识/短码。Token 必须由 CSPRNG 生成、至少满足 Wire Contract 长度，且不得出现在 URL、日志或遥测中。

Provider credential 必须有限有效，Revoke 必须按 operation ID 幂等。Hub 的 revoked 事实先持久化，再通知 Provider；Provider 不可达时不会恢复 Hub 授权，但残余 credential 窗口由 Provider 契约和后续故障测试约束。

## 结果

Hub 从五表和四个连接状态维度收敛为两表和一个三态策略生命周期。设备网络流量只在初次加入时经过 Hub，长期容量和故障域归 Provider。代价是 Hub 不提供设备在线视图；需要 online、业务状态或设备数据的消费者必须从 Provider 的契约获取。

如果未来确有“无 Provider 的长期 Hub 管理连接”产品目标，必须先给出消费者、故障模型和安全需求，再以新 ADR 引入；不得恢复旧代码作为预留。
