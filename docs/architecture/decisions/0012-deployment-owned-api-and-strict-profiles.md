# ADR 0012: 部署层拥有 API 监听，Hub 使用严格 Local/Cloud Profile

- 状态：Accepted
- 日期：2026-08-02

## 背景

Uvicorn 的内部监听地址与设备访问的公开 HTTPS 地址不是同一概念。Cloud 常由 Nginx、Ingress 或 Load Balancer 终止 TLS；把 host/port 同时用于 Uvicorn 和 mDNS 会错误地把内部端口写进设备契约。原配置还强制要求 `.env` 文件、用一个 reconciliation 周期驱动两个 Directory 任务，并由 `create_all()` 隐式初始化生产 Schema。

## 决策

1. ASGI host/port、TLS、forwarded-header trust 和反向代理由部署命令管理，Hub Settings 不包含 `api`。
2. `device_access.public_base_url` 是设备契约的唯一公开地址，必须为 HTTPS。mDNS hostname/port 从该 URL 派生，service type 固定，instance name 从稳定 `hub_id` 派生。
3. `EIDOLON_HUB_PROFILE=local|cloud` 选择两份完整配置；显式 YAML 路径只作为部署覆盖。Settings 使用 frozen、strict、extra-forbid 的 Pydantic Model。
4. `.env` 只在显式指定或默认文件存在时加载；Cloud 可只使用平台注入的环境变量。
5. `hub_id` 属于共享行为配置；Cloud `hub_instance_id` 必须由每副本唯一的 `EIDOLON_HUB_INSTANCE_ID` 提供。
6. Device Directory 始终为 DB-authoritative + in-memory write-through。在线投影和跨实例 cache refresh 使用独立周期；Local 不执行无意义的 cache DB polling。
7. Schema 使用 packaged Alembic revision。Local 可启动迁移；Cloud 由独立 Job 迁移，应用启动只检查 revision head。
8. OpenTelemetry 使用标准 `OTEL_EXPORTER_OTLP_ENDPOINT`。启用但 endpoint 缺失时启动失败，不静默关闭。
9. PostgreSQL 连接目标以 credential-free `persistence.dsn` 配置；用户名和密码使用固定环境变量。URL 由 SQLAlchemy 结构化合成，不拼接字符串。连接池显式配置 size、overflow、timeout 和 recycle。

## 后果

- 同一 Hub artifact 可通过 Profile 切换 Local/Cloud，HTTP/OpenAPI 契约不变。
- Nginx/Ingress 的内部端口不再泄漏到 Descriptor 或 mDNS。
- Cloud 发布流程必须先运行 migration Job，并为每个副本注入唯一 instance ID。
- 当前 Directory cache refresh 仍是周期性全量恢复；大规模目录应进一步采用 revision watermark 增量读取，但不引入 NATS 作为 Hub 内部正确性依赖。
