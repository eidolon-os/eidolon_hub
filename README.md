# eidolon-hub

Eidolon Hub 是 Eidolon OS 的设备接入与管理控制面。生产入口分成三个独立边界：

- Connection Plane：mDNS/DNS-SD/显式 URI 发现，以及 HTTPS、MQTT5 的认证、注册、心跳、租约和 Channel Signaling。
- Device Management Core：设备身份、WoT 风格 Manifest、审批/吊销、命令账本和持久化 Device Directory。
- Channel Orchestrator：按逻辑 Profile 请求外部 Provider，原样转交 opaque binding，并通过标准 `DataEnvelope` 与 Provider 交换管理数据。

LiveKit/WSS 是设备完成连接后的 Channel Provider，不是 Connector，也不参与在线判定。Hub 只配置 Provider 控制端点，不知道设备侧 URL、Room、Token、TURN 或 Codec；这些内容只存在于 Provider 生成的 opaque binding 中。

完整边界和数据流见 [三平面架构](docs/architecture/hub-three-plane.md)，兄弟项目的当前事实与迁移边界见 [Eidolon OS 集成图](docs/architecture/eidolon-os-integration.md)。协议源位于 `hub/contracts/schemas`，异步绑定位于 `hub/contracts/bindings/asyncapi.yaml`。

## 运行

```bash
uv sync --all-groups
cp config/.env.example config/.env
cp config/settings.example.yaml config/settings.yaml
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8082
```

Hub 不依赖 NATS 或 `eidolon_data`。本地默认使用 Hub 自有 SQLite；云端将 `persistence.adapter` 改为 `postgresql`，并通过 `EIDOLON_HUB_POSTGRES_DSN` 注入 DSN。启动需要三个至少 32 字节的凭据：

- `EIDOLON_HUB_LEASE_SECRET`
- `EIDOLON_HUB_MANAGEMENT_JWT_SECRET`
- `EIDOLON_HUB_PROVIDER_TOKEN`

可选 WAN MQTT 密码使用 `EIDOLON_HUB_MQTT_PASSWORD`。设置标准环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 后启用 trace、metric 和 log 的 OTLP/gRPC 批量输出；不设置时不外发。

## 生产 API

- `/api/connection/v1/descriptor|hello|proof|register|heartbeat|signals`：统一 Connection Contract 的 HTTPS Binding。
- `/api/device-management/v1/directory/{owner_scope}`：Owner 隔离的公共设备目录。
- `/api/device-management/v1/events/{owner_scope}`：按持久 stream position 增量读取 Owner 隔离的设备事件。
- `/api/device-management/v1/devices/{device_id}/approval|revocation`：设备归属、审批和吊销。
- `/api/device-management/v1/devices/{device_id}/channels/{profile_name}`：Provider 无关的 Channel Provision。
- `/api/device-management/v1/devices/{device_id}/commands` 与 `/commands/{command_id}`：可持久化的 Command/Ack/Result 闭环。
- `/api/provider/v1/data/inbound`：Provider 回送标准 `DataEnvelope` 的认证入口；Hub 向 Provider 的对应 HTTP API 发送命令 Envelope。

管理 API 使用 `aud=eidolon-hub` 的 HS256 JWT。`device-manager` 受 `owner_id` 限制，`hub-admin` 可管理未归属设备。

## 发现与跨子网

mDNS 仅用于同链路发布 Descriptor URI，并同时覆盖活跃 IPv4/IPv6 接口。复杂 VLAN/子网不做 mDNS 泛洪；设备在 Commissioning 时保存显式 HTTPS Descriptor URI，或使用标准单播 DNS-SD 的 PTR/SRV/TXT/A/AAAA 记录。

标准 mDNS SDK 不会自动把查询切换到 RFC 8766 Discovery Proxy。Discovery Proxy/SRP 属于网络基础设施；设备若要使用它，需要单播 DNS-SD/SRP 客户端能力。资源受限设备可以只实现 mDNS + HTTPS，并以显式 URI 作为跨子网兜底。

## 验证

```bash
./.venv/bin/python scripts/generate_contracts.py --check
./.venv/bin/lint-imports
./.venv/bin/ruff check hub tests scripts
./.venv/bin/python -m pytest -q
```

`eidolon_sdk`、`eidolon_data`、`nats-py` 和 LiveKit 均不在 Hub 的直接或传递依赖树中。旧 register/token、LiveKit 在线判定和 snapshot blackboard 运行时已从 Hub 删除。

## License

MIT
