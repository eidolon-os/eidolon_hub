# ADR 0019: Device Fact 与 Management Audit 是同一原子 Mutation

- 状态：Accepted
- 日期：2026-08-04
- 修订：ADR 0009、0018 中仅表述为 DB-first 的写入语义

## 问题

旧实现依次调用 `DeviceRepository.upsert`、`DeviceManagementEventSink.publish` 和内存 Directory Projector。前两步各自提交 SQLite 事务。如果进程在设备提交后、事件提交前失败，权威设备事实已经包含 request ID，但重试会因幂等命中直接返回，留下永久缺失的审计事件；如果失败发生在事件提交后、投影前，当前进程的 Directory 也无法由同一请求重试修复。

Hub 是 Device Registry 与 Policy Authority。设备管理事实、该请求的幂等标记和“谁在何时改变了什么”的审计不能发生分裂。

## 决策

1. Application 的所有 Device Mutation 只能调用窄 `DeviceMutationUnitOfWork.commit(expected, device, event)` Port。
2. SQLite Adapter 在同一事务中写入 `hub_devices` 和 `hub_events`；任一步失败时全部回滚。
3. `expected` 是 Use Case 读取到的完整不可变 Aggregate 快照。Adapter 在单进程写锁内重新读取并比较；陈旧请求返回 409 语义，不能覆盖更新后的事实。
4. `DeviceRepository` 只提供 Get、Enrollment Get 和 List，不暴露非原子 `upsert`。
5. `DeviceManagementEventLedger` 只向外提供有界增量读取，不暴露独立 Publish Port。
6. 内存 Directory 在事务提交后更新。幂等重试仍重新执行 Projector，从权威设备事实修复提交后、投影前的失败。
7. Provider Provision/Revoke 是跨服务调用，不纳入 SQLite 事务。Revoke 先原子提交 Hub revoked 事实和审计，再使用相同 operation ID 幂等通知 Provider；Provider 故障窗口仍由 ADR 0018 的有限 credential lease、显式重试和后续真实故障测试约束。
8. Approval/Revocation 的 `principal_id` 只能来自 Management Authorizer 返回的已验证 JWT `sub`，并参与 request fingerprint；`owner_id` 仍只表达目标 OS namespace。Enrollment 尚未经过管理认证，因此只记录 `untrusted-device:{device_id}`，不能解释为 Owner 或可信操作主体。
9. 所有 mutation fingerprint 都对操作名与命名参数的 canonical JSON 求 SHA-256；禁止使用换行、冒号等分隔符拼接，因为不同参数组合可产生同一字符串。

## 后果

- 空库 ORM 仍只有 `hub_devices` 与 `hub_events`；`hub_events` 显式保存非空、可索引的 `principal_id`，不需要 migration 或兼容代码。
- 审计流可以作为 Kernel、管理端和其他 metadata consumer 的可靠事实游标，但不是通用 System Bus。
- 内存投影继续是可丢弃、可重建的热读副本，不参与授权。
- Local-only 文件锁保证只有一个 Hub 进程；Unit of Work 的进程内异步写锁负责该进程内 Mutation 串行化，expected snapshot 负责拒绝 read-check-write 的陈旧结果。
