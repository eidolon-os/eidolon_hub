# 测试推动的架构发现

1. mDNS advertiser 与 MQTT client 只有 start/stop 形似，能力并不相同；共同 `ConnectionConnector` Port 是伪抽象，已删除。
2. WAN bootstrap 用稳定 HTTPS URI 已足够；MQTT 的 QoS/长连接价值属于实际 Channel backend，不属于 Hub 注册。
3. 进程内 grant mailbox 无法恢复且增加状态；审批后的有界 Handoff 直接返回 Provider assignment 更短、更一致。
4. 后台 desired-state worker/SQL claim 把 Provider 资源策略带入 Hub。Hub 改为只透传必要设备事实，并保留通用 assignment 契约校验。
5. Channel Assignment metadata 和 Lifecycle callback 在代码中只有写入、没有任何设备管理消费者；它们复制 Provider 状态机，已连同表、Repository、Use Case、Schema 和入站 Router 整体删除。
6. Directory cache 必须 DB-first、启动恢复；它只优化热读，不能承担授权。投影可直接由 `hub_devices` 重建，不需要第二张持久 Directory 表。
7. 同时维护 SQLite/PostgreSQL、Profile、authority fencing 和跨实例 cache refresh，会把所有兄弟项目拖入尚不需要的部署复杂度。保留 Repository Port，但运行 Adapter 收敛为 SQLite，并用文件锁明确单进程所有权。
8. `eidolon_channel` 尚未实现新 Provider 契约，本轮没有修改兄弟项目，不能声称 Eidolon OS 整栈已切换。
9. SQLite 不提供行级 `SELECT FOR UPDATE`；未参与条件更新的 `version` 字段是虚假并发保障。旧 Challenge 曾改用条件原子更新，随后该认证流程已因 ADR 0018 整体删除。
10. 人工 Approval 后设备立即由 Provider 接管，Hub 的 Challenge/Proof、持久 Session、Heartbeat/Lease、online 和周期投影没有剩余授权消费者，是重复连接生命周期，已整体删除。
11. retrieval token 只能关联 Enrollment 调用方，不能证明设备物理真实性。小程序配网必须有二维码、近场或物理确认；这是一项产品安全契约，不能靠增加 Hub Session 状态机替代。
12. Device Fact 与 Management Audit 分开提交会产生“事实已生效、审计永久缺失”的崩溃窗口，幂等早退还会阻止投影修复。生产写入口已收敛为一个 Device Mutation Unit of Work：同一事务提交 Device、request 幂等标记和 Audit，expected snapshot 拒绝陈旧覆盖，幂等重试重新投影。
13. Owner 是 Kernel 的根 Security/Namespace principal，不等于账号资料或 Companion。Hub 的 Device→Owner Admission 和 Kernel 的 Device→Companion Mount 必须保持单一权威；用 Hub 保存 `companion_id` 或让 Hub 回调 Kernel 都会重新制造跨项目双写。
14. Owner scope 与操作审计主体不能混为一个字段。Hub 的 `owner_id` 决定设备归属，而 Approval/Revocation 的 `principal_id` 必须来自已验证 JWT `sub`；否则事件只能说明改了哪个 Owner，无法证明是谁执行，且 request payload 可以伪造审计。
15. 内容幂等不能依赖字符串分隔符。冒号拼接可让 `owner:a + principal` 与 `owner + a:principal` 产生相同 fingerprint；Enrollment/Approval/Revocation 已统一使用 canonical JSON + SHA-256，并由 delimiter-collision 回归用例锁定。
