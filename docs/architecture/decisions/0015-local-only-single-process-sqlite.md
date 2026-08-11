# ADR 0015: Hub 收敛为 Local-only 单进程 SQLite

- 状态：Accepted；持久 Session/Directory 表部分由 ADR 0018 修订
- 日期：2026-08-03
- 取代：ADR 0006；ADR 0009/0012 中的 PostgreSQL、Local/Cloud Profile、多实例和 fencing 决策

## 背景

> Local-only、单进程和独占 SQLite 仍有效；当前两表模型与内存投影以 [ADR 0018](0018-hub-is-onboarding-and-provider-handoff.md) 为准。

Eidolon OS 不止 Hub 一个子项目。过早要求每个项目同时支持 Local/Cloud，会引入两套配置、数据库驱动、迁移运行方式、多实例 authority、跨实例缓存一致性和部署验证，而当前代码与真实 consumer 尚未证明这些复杂度有产品必要性。保留这些未被使用的分支会扩大测试矩阵，也容易让“接口抽象”被误认为“已经具备云端运行能力”。

Hub 仍需要清晰分层、可测试的 Repository Port、数据库权威源和 Directory 内存热读；这些设计并不依赖双部署模式。

## 决策

1. Hub 产品运行形态只有 Local 单进程；只有一个严格的 `config/settings.yaml`，不再提供 Profile selector。
2. Hub 运行时数据库只有独占 SQLite 文件，默认路径为 `$EIDOLON_STATE_ROOT/hub/eidolon-hub.sqlite3`。
3. Composition 启动时获取 `<database>.lock` 的非阻塞独占文件锁。共享同一数据库的第二个 Hub 进程立即失败，不尝试主从切换。
4. 删除 PostgreSQL/asyncpg、pool 配置、独立 migration CLI 和 Cloud-only tests。
   数据库 migration 机制随后也已删除；当前语义见 ADR 0016 与 Device Control Subsystem 标尺。
5. 删除 `DeviceAuthorityLease`、Authority Repository、`hub_instance_id`、`fencing_token` 和跨实例 Directory refresh。设备在线只由本进程持久 Session 的状态与 expiry 推导。
6. 保留 Domain/Application/Port/Adapter/Composition 依赖方向。上层仍不读取 SQLite 或 SQLAlchemy 细节；这是所有权边界，不是对第二个数据库实现的承诺。
7. Device Directory 继续 DB-first + in-memory write-through：启动 hydrate，热读在内存，SQLite 是可恢复权威源。
8. 本 ADR 当时暂时保留 `tenant_id`；该身份决策已由 [ADR 0016](0016-device-state-storage-and-query-contract.md) 取代，当前设备不能提交 Authority Scope。

## 结果

配置、依赖、持久化和测试矩阵显著缩小，当前能力与文档一致。SQLite 文件锁适合本机文件系统和单进程所有权，不提供网络文件系统、多主、HA、rolling upgrade 或跨主机 fencing。若未来真实需求要求多实例，必须以新的 ADR、故障模型和端到端证据重新引入合适的数据库与协调机制，不能只新增一个配置分支。
