# ADR 0016：设备状态、Local 存储与查询契约

- 状态：Accepted；状态名和 online 维度由 ADR 0018 修订
- 日期：2026-08-03
- 补充：ADR 0013、0015

## 背景

> Get/List 选择仍有效；当前 lifecycle 已收敛为 `pending-approval / approved / revoked`，Hub 不再表达 online，详见 [ADR 0018](0018-hub-is-onboarding-and-provider-handoff.md)。

Hub 已收敛为 Local-only Device Manager，但原模型仍用 `approved + revoked` 两个布尔值
表达生命周期，设备身份 Wire Contract 允许设备提交 `tenant_id`，公共目录暴露 Session ID，
项目间查询也尚未形成稳定的 Get/List 契约。

当前兄弟项目代码显示：Admin 需要 Owner 列表和设备详情；Agent 需要按 Owner、在线性和能力
筛选设备；Channel 在建立会话时按 device ID 解析上下文。这些都是低频控制面查询。

## 决策

1. 设备生命周期为 `pending-approval -> active -> revoked`，revoked 为终态。
2. Session 和 online 是独立维度，不进入设备生命周期枚举；Channel lifecycle 不属于 Hub 领域。
3. 删除设备身份和 Provider 契约中的 `tenant_id`；Local-only Hub 不存在设备可选择的 Authority Scope。
4. SQLite 继续作为独占权威库；Directory 继续采用 SQLite + in-memory write-through 投影。
5. Domain、Wire Contract 与当前 ORM 使用单一 lifecycle enum。开发期不保留旧双布尔结构的 migration 或转换逻辑；不匹配当前 ORM 的数据库直接拒绝启动。
6. 项目间读取采用 HTTP/JSON Get 与有界 List。List 使用结构化 filter、limit 和稳定 cursor。
7. 不建立独立 Search RPC；可选 `q` 是 List 的 UI 过滤条件。
8. 不引入 gRPC。当前无高频/streaming 查询证据，小程序和浏览器又是直接消费者；Protobuf、
   stub generation 和 gRPC-Web proxy 会形成第二契约工具链。
9. HTTP Router 只依赖 `GetDevice`、`ListDevices` 等 Application Query，不直接访问 Repository。

## 结果

状态组合不再出现 `approved=true, revoked=true` 等非法值；设备不能选择授权范围；公共目录
不再泄露 Session 标识或二次编码 JSON。所有 Eidolon OS 项目获得相同、可分页、可缓存的
设备查询契约，同时保留未来在 Adapter 层替换 transport 的可能性，而不提前承担 gRPC 复杂度。
