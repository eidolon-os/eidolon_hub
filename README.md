# eidolon-hub

Eidolon Hub 是 Eidolon OS 的设备接入与管理控制面，也是对外契约化的 Device Bus。它拥有设备身份、认证会话、能力目录、审批/吊销、命令账本、通用 Channel 元数据和事件流；它不拥有设备通信后端或媒体资源。

当前边界：

- Device Access：mDNS 只发布 HTTPS Descriptor URI；设备通过 HTTPS 建立认证 `DeviceSessionLease`、注册、心跳和关闭。WAN 设备在 Commissioning 时保存同一个固定 URI。
- Device Management：公开 Owner scoped Device Directory、审批/吊销、Command/Ack/Result 和持久事件流。
- Channel Contract：设备注册并获批后主动 Acquire。Hub 把必要设备上下文交给外部 Channel Provider，并把 Provider 返回的 opaque binding 直接返回设备。
- Provider Bridge：Provider 承载 WSS、MQTT、LiveKit 或未来后端，并通过标准 `DataEnvelope` 与 Hub 交换普通管理数据；音视频不进入 Hub。

Hub 没有 MQTT、LiveKit、NATS、`eidolon_data` 或 `eidolon_sdk` 运行时依赖。MQTT 若被采用，是外部 Channel Provider 的可靠数据后端，不是 Hub 的发现、注册或在线协议。

完整设计见 [Hub 架构](docs/architecture/hub-three-plane.md)、[ADR 0011](docs/architecture/decisions/0011-device-session-and-direct-channel-acquisition.md) 和 [测试报告](docs/testing/reports/README.md)。Wire Contract 的源位于 `hub/contracts/schemas`。

## 运行

```bash
uv sync --all-groups
cp config/.env.example config/.env
export EIDOLON_HUB_PROFILE=local
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8082
```

ASGI 监听地址、端口、TLS 和可信代理由 Uvicorn/Gunicorn、Nginx、Ingress 等部署层管理，不属于 Hub 行为配置。`public_base_url` 必须指向设备真正可访问的 TLS 终止地址；上面的 Uvicorn 命令只启动内部 HTTP upstream，不能直接满足设备 HTTPS 契约。

`EIDOLON_HUB_PROFILE=local|cloud` 选择 `config/settings.local.yaml` 或 `config/settings.cloud.yaml`，未指定时为 Local。部署需要自定义完整配置时，使用 `EIDOLON_HUB_SETTINGS_YAML=/path/settings.yaml` 覆盖。`.env` 只方便本地开发；Cloud 可直接注入环境变量，不要求磁盘存在 `.env`。启动需要三个至少 32 字节的凭据：

- `EIDOLON_HUB_LEASE_SECRET`
- `EIDOLON_HUB_MANAGEMENT_JWT_SECRET`
- `EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN`

Local 使用 Hub 自有 SQLite 并在启动时执行版本化迁移。Cloud 的非秘密 PostgreSQL DSN（host、port、database 和 SSL query）位于 `persistence.dsn`，DSN 禁止包含凭据；用户名和密码分别由 `EIDOLON_HUB_POSTGRES_USER`、`EIDOLON_HUB_POSTGRES_PASSWORD` 注入。发布前以独立 Job 执行 `uv run eidolon-hub-migrate`；应用实例只校验 Schema revision，不自行改表。每个 Cloud 副本还必须注入唯一的 `EIDOLON_HUB_INSTANCE_ID`。

Observability 是真实的 OpenTelemetry OTLP/gRPC 接口：当前导出 HTTP Trace、请求计数/延迟 Metric 和 Python Log。Local profile 明确关闭；Cloud profile 启用并要求 `OTEL_EXPORTER_OTLP_ENDPOINT`，缺失时启动失败而不是静默降级。

## 生产 API

- `/api/device-access/v1/descriptor|hello|proof|register|heartbeat|close`：HTTPS Device Access Contract。
- `/api/device-access/v1/channels/acquire`：认证设备主动获取 Provider channel binding。
- `/api/device-management/v1/directory/{owner_scope}`：公共设备目录。
- `/api/device-management/v1/events/{owner_scope}`：按 stream position 增量读取设备事件。
- `/api/device-management/v1/devices/{device_id}/approval|revocation`：设备审批与吊销。
- `/api/device-management/v1/devices/{device_id}/commands` 与 `/commands/{command_id}`：命令闭环。
- `/api/provider/v1/data/inbound`：Provider 回送标准 `DataEnvelope`。
- `/api/provider/v1/channels/lifecycle`：Provider 报告 Channel active/closed/failed。

Hub 只配置 `channel_provider.contract_url`，固定调用 Provider 的 `/device-channels/acquire` 和 `/data/envelopes` 契约。Hub 不解析、不持久化 opaque binding。只有 loopback Provider 可使用 HTTP，远端地址必须使用 HTTPS。

## 发现与跨子网

mDNS 仅用于同链路发布 Descriptor URI，并覆盖活跃 IPv4/IPv6 接口。复杂 VLAN/子网不泛洪 mDNS；设备使用 Commissioning 保存的显式 HTTPS URI。Unicast DNS-SD、RFC 8766 Discovery Proxy 和 SRP 属于设备/网络侧发现能力，不由 Hub 实现，也不会让标准 mDNS SDK 无感知地跨子网工作。

## 验证

```bash
uv run python scripts/generate_contracts.py --check
uv run lint-imports
uv run ruff check hub tests scripts
uv run pytest -q
```

## License

MIT
