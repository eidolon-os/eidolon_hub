# Hub 重构测试矩阵

## 自动化矩阵

| 场景 | 层级 | 主要测试 | 已验证的不变量 |
|---|---|---|---|
| Manifest/WoT capability | Unit + property contract | `test_domain_invariants.py`, `test_json_schema_contracts.py` | canonical JSON、content revision、schema boundaries |
| Command Ledger | Unit + functional | `test_command_state_machine.py`, `test_send_command.py`, external provider flow | TTL、所有状态转移、request-id binding、Ack/Result |
| 多 Connector/切换 | Unit + conformance | `test_connections.py`, `test_close_connection.py`, connector conformance | online aggregation、priority、只在最后路径关闭时撤 Channel |
| HTTPS Connection | Functional | `test_https_connection_flow.py` | Hello→Proof→Register→Heartbeat、replay、server connector identity |
| WAN MQTT5 | Contract + functional | `test_mqtt_connection_contract.py`, `test_mqtt_connection_flow.py` | narrow operations、Topic identity、opaque grant、无业务 data |
| mDNS | Adapter unit + conformance | `test_discovery_adapters.py`, connector conformance | 多 IPv4/IPv6、动态更新、无地址 fail closed |
| 显式 URI | Adapter unit | `test_discovery_adapters.py` | HTTPS only、commissioned URI matching |
| Unicast DNS-SD | Adapter unit | `test_discovery_adapters.py` | PTR/SRV/TXT/A/AAAA、不编码 DNS packet |
| Channel Orchestrator | Unit + provider conformance | `test_channel_orchestrator.py`, `test_use_case_boundaries.py`, provider conformance | profile selection、mismatch compensation、renew/revoke |
| WSS-only management path | Functional | `test_external_channel_provider_flow.py` | opaque WSS binding、Command→Ack→Result、无 LiveKit 依赖 |
| Realtime provider path | Functional | `test_external_channel_provider_flow.py` | MQTT 只 relay grant，Provider binding 原样到设备 |
| DataEnvelope | Unit + contract | `test_data_channel_bridge.py`, `test_data_envelope.py`, JSON Schema tests | typed payload、device/channel/TTL、cursor dedupe |
| Device Directory | Component + unit | `test_device_directory_projection.py`, `test_memory_directory.py` | restart restore、DB-first cache、reconcile、owner transfer、无 secret |
| Multi-Hub fencing | Persistence component | `test_persistence_adapters.py` | monotonic token、stale owner rejection、expiry acquire |
| Management RBAC | Adapter unit | `test_management_jwt.py` | role、audience、expiry、owner、approved/revoked |
| Challenge security | Unit + functional | `test_connection_security.py`, HTTPS flow | nonce binding、P-256 proof、single consume/replay rejection |
| Persistence Ports | Component contract | `test_persistence_adapters.py` | Hub-owned SQLite round trip；PostgreSQL 共用 Adapter、上层无数据库类型 |
| Device event stream | Persistence + OpenAPI component | persistence adapters、composition routes | owner filter、durable position、idempotent event ID、bounded page |
| Telemetry/redaction | Adapter unit + architecture | `test_observability.py`, dependency boundary tests | empty endpoint disabled、safe attributes、production config no provider detail |
| SDK/Data/NATS boundaries | Architecture | `test_dependency_boundaries.py`, Import Linter | no SDK/Data/NATS dependency、core no infrastructure |
| Provider HTTP binding | Adapter + component | `test_http_provider_transport.py`, composition routes | bearer auth、provision/data envelope、无 Provider 细节泄漏 |
| Generated Contracts | Contract | `test_json_schema_contracts.py`, generator `--check` | Draft 2020-12、golden examples、deterministic generated tree |

## 固定命令

```bash
./.venv/bin/ruff check hub tests scripts
./.venv/bin/ruff format --check hub tests scripts
./.venv/bin/lint-imports
./.venv/bin/python scripts/generate_contracts.py --check
./.venv/bin/python -m pytest -q
./.venv/bin/python -m pytest -q tests/unit tests/component tests/functional \
  --cov=hub.application --cov-branch --cov-fail-under=90
uv tree --frozen --depth 1 --no-dev
```

## 需要部署环境完成的验收

下列故障不能由纯单进程 Fake 完整证明，发布前在 compose/真实基础设施环境执行：

| 故障注入 | 期望 |
|---|---|
| MQTT Broker restart | Connector backoff 重连、QoS1 重订阅、注册/lease request 幂等 |
| SQLite/PostgreSQL restart | in-flight use case fail closed，持久化 cursor/Directory 不倒退 |
| Provider restart | 已到期 channel 不可用，renew/provision 可恢复，无 orphan lease |
| Hub/PostgreSQL rolling restart | Directory 保留，cache rehydrate，authority fencing 阻止旧 instance 写入 |
| 配置轮换 | 新 Grant 使用新 Provider 配置；历史 opaque binding 不在 Hub 保存 |

Broker ACL、TLS/mTLS、证书撤销和 OTLP collector 可用性必须使用部署实际产品验证，不能用单元测试结果替代。
