# Hub 重构测试矩阵

## 自动化矩阵

| 场景 | 层级 | 主要测试 | 已验证的不变量 |
|---|---|---|---|
| Manifest/WoT capability | Unit + property contract | `test_domain_invariants.py`, `test_json_schema_contracts.py` | canonical JSON、content revision、schema boundaries |
| Command Ledger | Unit + functional | `test_command_state_machine.py`, `test_send_command.py`, external provider flow | TTL、所有状态转移、request-id binding、Ack/Result |
| 多 Connector/切换 | Unit + conformance | `test_connections.py`, `test_close_connection.py`, connector conformance | online aggregation、fencing、Connection 只修改自有事实 |
| HTTPS Connection | Functional | `test_https_connection_flow.py` | Hello→Proof→Register→Heartbeat、replay、server connector identity |
| WAN MQTT5 | Contract + functional + optional infrastructure | `test_mqtt_connection_contract.py`, `test_mqtt_connection_flow.py`, `test_cloud_infrastructure.py` | inbound narrow operations、Topic identity、outbound opaque grant、无业务 data |
| mDNS | Adapter unit + conformance | `test_discovery_adapters.py`, connector conformance | 多 IPv4/IPv6、动态更新、无地址 fail closed |
| 显式 URI | Adapter unit | `test_discovery_adapters.py` | HTTPS only、commissioned URI matching |
| Unicast DNS-SD | Adapter unit | `test_discovery_adapters.py` | PTR/SRV/TXT/A/AAAA、不编码 DNS packet |
| Channel desired state | Unit + provider conformance | `test_channel_provider_reconcile.py`, `test_channel_provider_sync_contract.py` | approval gate、retry operation、pending 补投、lease generation 轮换、SQL claim、backoff、disconnect/revoke convergence |
| Channel lifecycle | Unit + HTTP contract | `test_channel_lifecycle.py`, `test_provider_gateway_contract.py` | pending/active gate、terminal close/failure、device binding、Provider auth |
| Provider management path | Functional | `test_external_channel_provider_flow.py` | real ASGI HTTP contract、opaque binding、active→Command→Ack→Result |
| DataEnvelope | Unit + contract | `test_data_channel_bridge.py`, `test_data_envelope.py`, JSON Schema tests | typed payload、device/channel/TTL、cursor dedupe |
| Device Directory | Component + unit | `test_device_directory_projection.py`, `test_memory_directory.py`, `test_directory_worker.py` | restart restore、DB-first cache、自然 lease 过期、无变化 revision 稳定、owner transfer、无 secret |
| Multi-Hub fencing | Persistence component | `test_persistence_adapters.py` | monotonic token、stale owner rejection、expiry acquire |
| Management RBAC | Adapter unit | `test_management_jwt.py` | role、audience、expiry、owner、approved/revoked |
| Challenge security | Unit + functional | `test_connection_security.py`, HTTPS flow | nonce binding、P-256 proof、single consume/replay rejection |
| Persistence Ports | Component + optional infrastructure | `test_persistence_adapters.py`, `test_cloud_infrastructure.py` | Hub-owned SQLite round trip；真实 PostgreSQL 仅在 dedicated DSN 可用时计为通过 |
| Device event stream | Persistence + OpenAPI component | persistence adapters、composition routes | owner filter、durable position、idempotent event ID、bounded page |
| Telemetry/redaction | Adapter unit + architecture | `test_observability.py`, dependency boundary tests | empty endpoint disabled、safe attributes、production config no provider detail |
| SDK/Data/NATS boundaries | Architecture | `test_dependency_boundaries.py`, Import Linter | no SDK/Data/NATS dependency、core no infrastructure |
| Provider HTTP binding | Adapter + contract + E2E | provider sync/gateway contract、composition routes、`test_local_contract_e2e.py` | bearer auth、fixed routes、sync/lifecycle/data envelope、无 Provider 细节泄漏 |
| Generated Contracts | Contract | `test_json_schema_contracts.py`, generator `--check` | Draft 2020-12、golden examples、deterministic generated tree |
| Local/Cloud artifact parity | Functional | `test_deployment_mode_switch.py` | identical OpenAPI/schema、config-only Adapter/Connector selection、无隐式数据桥 |
| Local black-box contract E2E | E2E | `test_local_contract_e2e.py` | production composition、P-256、SQLite+memory directory、real TCP Provider、restart recovery |
| Composition/runtime ownership | Unit + component + architecture | `test_runtime.py`, `test_composition_routes.py`, Import Linter | UTC Clock、安全且唯一的 prefixed ID、按子系统装配、Core 依赖方向不变 |

## 固定命令

```bash
./.venv/bin/ruff check hub tests scripts
./.venv/bin/ruff format --check hub tests scripts
./.venv/bin/lint-imports
./.venv/bin/python scripts/generate_contracts.py --check
./.venv/bin/python -m pytest -q
./.venv/bin/python -m pytest -q tests/unit tests/component tests/functional tests/e2e \
  --cov=hub.application --cov-branch --cov-fail-under=90
uv tree --frozen --depth 1 --no-dev
```

## 需要部署环境完成的验收

下列故障不能由纯单进程 Fake 完整证明，发布前在 compose/真实基础设施环境执行：

| 故障注入 | 期望 |
|---|---|
| MQTT Broker restart | Connector backoff 重连、QoS1 重订阅、注册/lease request 幂等 |
| SQLite/PostgreSQL restart | in-flight use case fail closed，持久化 cursor/Directory 不倒退 |
| Provider restart | 注册事实不回滚；desired-state sync 按退避恢复，pending binding 可重发，无 orphan lease |
| Hub/PostgreSQL rolling restart | Directory 保留，cache rehydrate，authority fencing 阻止旧 instance 写入 |
| 配置轮换 | 新进程只访问新 contract URL；历史 opaque binding 不在 Hub 保存 |

Broker ACL、TLS/mTLS、证书撤销和 OTLP collector 可用性必须使用部署实际产品验证，不能用单元测试结果替代。

本轮逐类执行结果和证据等级见 [`docs/testing/reports/`](../testing/reports/README.md)。
