# eidolon-hub

Eidolon Hub 是 Eidolon OS 的设备接入与管理控制面。生产入口分成三个独立边界：

- Connection Plane：mDNS/DNS-SD/显式 URI 发现，以及 HTTPS、MQTT5 的认证、注册、心跳、租约和 Channel Signaling。
- Device Management Core：设备身份、WoT 风格 Manifest、审批/吊销、命令账本和持久化 Device Directory。
- Channel Control：把已持久化的设备 desired state 异步同步给外部 Provider，原样转交 opaque binding，并通过标准 `DataEnvelope` 与 Provider 交换管理数据。

LiveKit/WSS 是设备完成连接后的 Channel Provider，不是 Connector，也不参与在线判定。Hub 只配置 Provider 控制端点，不知道设备侧 URL、Room、Token、TURN 或 Codec；这些内容只存在于 Provider 生成的 opaque binding 中。

完整边界和数据流见 [三平面架构](docs/architecture/hub-three-plane.md)，Channel 决策见 [ADR 0010](docs/architecture/decisions/0010-channel-provider-contract-boundary.md)，稳定性闭环见 [测试策略](docs/testing/test-strategy.md)，本轮证据见 [验收报告索引](docs/testing/reports/README.md)。协议源位于 `hub/contracts/schemas`，异步绑定位于 `hub/contracts/bindings/asyncapi.yaml`。

## 运行

```bash
uv sync --all-groups
cp config/.env.example config/.env
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8082
```

`config/settings.yaml` 是唯一受版本控制的非秘密配置；`config/.env` 只承载密钥且不提交。mDNS 位于 `connection_plane.mdns`，表示 Connection Plane 的发现发布器，不产生设备连接租约。Hub 没有任何 LiveKit 配置，实时媒体连接细节全部由外部 Provider 通过 opaque binding 提供。

Hub 不依赖 NATS 或 `eidolon_data`。本地默认使用 Hub 自有 SQLite；云端将 `persistence.adapter` 改为 `postgresql`，并通过 `EIDOLON_HUB_POSTGRES_DSN` 注入 DSN。启动需要三个至少 32 字节的凭据：

- `EIDOLON_HUB_LEASE_SECRET`
- `EIDOLON_HUB_MANAGEMENT_JWT_SECRET`
- `EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN`

可选 WAN MQTT 密码使用 `EIDOLON_HUB_MQTT_PASSWORD`。设置标准环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 后启用 trace、metric 和 log 的 OTLP/gRPC 批量输出；不设置时不外发。

## 生产 API

- `/api/connection/v1/descriptor|hello|proof|register|heartbeat`：统一 Connection Contract 的 HTTPS Binding；`GET /signals/{device_id}` 只负责向设备中继 Provider grant。
- `/api/device-management/v1/directory/{owner_scope}`：Owner 隔离的公共设备目录。
- `/api/device-management/v1/events/{owner_scope}`：按持久 stream position 增量读取 Owner 隔离的设备事件。
- `/api/device-management/v1/devices/{device_id}/approval|revocation`：设备归属、审批和吊销。
- `/api/device-management/v1/devices/{device_id}/commands` 与 `/commands/{command_id}`：可持久化的 Command/Ack/Result 闭环。
- `/api/provider/v1/data/inbound`：Provider 回送标准 `DataEnvelope` 的认证入口。
- `/api/provider/v1/channels/lifecycle`：Provider 报告通用 Channel active/closed/failed；只有 active lease 可承载命令和上行数据。

Hub 只配置 `channel_provider.contract_url`。它固定调用 Provider 的 `/device-channels/sync` 与 `/data/envelopes` 契约；注册和审批事实先落库，后台数据库 claim 驱动幂等同步，Provider 故障不会回滚设备事实。只有 loopback Provider 可使用明文 HTTP，远端地址必须使用 HTTPS。

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
