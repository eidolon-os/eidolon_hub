# ADR 0009: 数据库权威源与内存热投影

- Status: accepted
- Date: 2026-08-01

## Decision

Hub 自有 Persistence Port 由同一套 SQLAlchemy Adapter 实现。本地配置创建独立 SQLite（WAL），云端配置连接 PostgreSQL；Domain、Application 和 HTTP Router 不知道 adapter 或 DSN。

强状态采用 DB-first/write-through：Device、审批/吊销、Command Ledger、Challenge、Connection/Authority Lease、Channel Lease/Cursor 和事件日志只有数据库提交成功后才算生效。PostgreSQL 行锁与 fencing token 负责多实例竞争。

Device Directory 可启用进程内不可变热投影。它在启动时 hydrate，写入时数据库先行，并周期性从数据库按完整快照对账其他实例变化。内存不是权威源，数据库失败不得通过延迟写回掩盖。

## Consequences

普通 Directory 查询不访问数据库，同时重启后可恢复。Cloud 每个实例的展示可能在对账窗口内短暂滞后，但授权、吊销、命令和 fencing 不使用该软缓存，因此正确性不受该窗口影响。若以后根据 profiling 增加缓存，必须逐项说明容许陈旧时间与失效策略。
