# ADR 0009: 数据库权威源与内存热投影

- Status: accepted
- Date: 2026-08-01
- Revised: 2026-08-04 by ADR 0015, ADR 0017 and ADR 0018

## Decision

Hub 自有 Persistence Port 由 SQLAlchemy SQLite Adapter 实现，创建独立 SQLite（WAL）；Domain、Application 和 HTTP Router 不知道数据库路径或 SQLAlchemy 类型。Port 边界保留，但当前 Composition 只有一个实现。

强状态采用 DB-first：Device、审批/吊销和管理事件只有数据库提交成功后才算生效。同一数据库由进程文件锁确保只有一个写入 Hub。ADR 0017 已删除 Channel Assignment metadata；ADR 0018 又删除 Challenge、Session 和持久 Directory projection。

Device Directory 使用进程内不可变热投影。它在启动时直接由 `hub_devices` hydrate，变更时数据库先行；内存不是权威源，也不对应第二张持久投影表。单写者模式不运行跨实例周期对账。

## Consequences

普通 Directory 查询不访问数据库，同时重启后可恢复。授权、吊销和 Handoff 不使用该软缓存。若以后根据 profiling 增加其他缓存，必须逐项说明容许陈旧时间与失效策略。
