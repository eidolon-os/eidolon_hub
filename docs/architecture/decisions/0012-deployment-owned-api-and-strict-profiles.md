# ADR 0012: 部署层拥有 API 监听，Hub 使用严格配置

- 状态：Accepted；Device Access 命名和 Directory 写入模型由 ADR 0018 修订
- 日期：2026-08-02
- 修订：2026-08-03，内置 OpenTelemetry 决策由 ADR 0014 取代；Local/Cloud Profile、PostgreSQL 和多实例条目由 ADR 0015 取代；Schema migration 条目由当前 Device Control Subsystem 标尺取代

## 背景

Uvicorn 的内部监听地址与设备访问的公开 HTTPS 地址不是同一概念。Cloud 常由 Nginx、Ingress 或 Load Balancer 终止 TLS；把 host/port 同时用于 Uvicorn 和 mDNS 会错误地把内部端口写进设备契约。原配置还强制要求 `.env` 文件、用一个 reconciliation 周期驱动两个 Directory 任务，并由 `create_all()` 隐式初始化生产 Schema。

## 决策

1. ASGI host/port、TLS、forwarded-header trust 和反向代理由部署命令管理，Hub Settings 不包含 `api`。
2. `onboarding.public_base_url` 是设备契约的唯一公开地址，必须为 HTTPS。mDNS hostname/port 从该 URL 派生，service type 固定，instance name 从稳定 `hub_id` 派生。
3. Settings 使用单一 `settings.yaml` 和 frozen、strict、extra-forbid 的 Pydantic Model；显式 YAML 路径只作为本地部署覆盖。
4. `.env` 只在显式指定或默认文件存在时加载，并只保存 Secret，不选择运行 Profile。
5. Device Directory 是由 DB-authoritative `hub_devices` 重建的内存投影；启动 hydrate，不运行跨实例 cache polling，也不持久化第二份投影。
6. 当前 ORM 是唯一 Schema；空库直接建表，非空库严格校验，不提供 migration 或旧结构转换。
7. Persistence 只配置 SQLite 文件路径；进程必须先获取对应文件锁。

## 后果

- Nginx/Ingress 的内部端口不再泄漏到 Descriptor 或 mDNS。
- 未知配置和已废弃的 Profile/PostgreSQL/instance 字段会 fail closed，不会被静默忽略。
- 本地 SQLite 启动路径简单、事实唯一；代价是明确不支持 Hub 多实例共享数据库。
