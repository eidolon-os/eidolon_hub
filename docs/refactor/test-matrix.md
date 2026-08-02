# Hub 重构测试矩阵

| 能力/风险 | 层级 | 当前测试 | 验证点 |
|---|---|---|---|
| Layer boundary | Architecture | `test_dependency_boundaries.py` | Core 纯净、无 Connector/MQTT/NATS/SDK/Data/LiveKit、opaque 不持久化 |
| Device Session | Domain + Unit + Functional | `test_sessions.py`, `test_use_case_boundaries.py`, `test_https_device_access_flow.py` | proof、lease、heartbeat sequence、close、expiry、fencing |
| Registration/ownership | Unit + Component | `test_register_device.py`, `test_device_lifecycle.py` | identity binding、request idempotency、approval/revoke、session close |
| Directory memory+DB | Unit + Component | `test_memory_directory.py`, `test_device_directory_projection.py` | DB-first、hydrate、cross-instance refresh、natural expiry、owner transfer |
| Deployment Settings | Unit + Architecture | production config/profile tests | no API listen coupling、strict/extra-forbid、Local/Cloud selector、optional dotenv、unique cloud instance、OTEL fail-closed |
| Schema migration | Unit + Cloud Functional | database runtime/cloud infrastructure tests | packaged Alembic revision、idempotency、head check、fresh PostgreSQL Schema |
| Direct Acquire | Unit + Contract | `test_acquire_device_channels.py`, `test_channel_provider_acquisition_contract.py` | active approved session、typed context、provider response validation、opaque relay、outage isolation |
| Provider lifecycle/data | Unit + Contract + Functional | lifecycle/data bridge/provider gateway/external provider tests | pending→active authority、Bearer boundary、cursor、Command/Ack/Result |
| Local mode | Component + Functional | composition/HTTPS/external provider tests | SQLite、mDNS optional、production assembly |
| Cloud mode | Functional | `test_cloud_infrastructure.py` | PostgreSQL 18 round-trip、concurrent authority fencing |
| Local/Cloud parity | Functional | `test_deployment_mode_switch.py` | identical OpenAPI paths/schemas、config-only adapter switch |
| Black-box contract | E2E | `test_local_contract_e2e.py` | P-256→register→approve→acquire→command result→restart recovery |

尚未完成且不能标记通过：生产 TLS/mTLS、DNS/VLAN 环境、Provider/DB restart、网络分区、rolling upgrade、兄弟项目 conformance。
