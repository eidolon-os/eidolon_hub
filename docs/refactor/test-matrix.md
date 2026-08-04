# Hub 重构测试矩阵

| 能力/风险 | 层级 | 当前测试 | 验证点 |
|---|---|---|---|
| Layer boundary | Architecture | `test_dependency_boundaries.py` | Core 纯净；无 Connector/Session/MQTT/NATS/SDK/Data/LiveKit/Command |
| Enrollment | Domain + Unit + Functional | enrollment/token/HTTPS tests | 高熵 Token、只存 hash、窗口、内容幂等、Device ID 冲突 |
| Approval policy | Unit + Component | lifecycle/persistence tests | pending→approved→revoked、Owner binding、终态、过期拒绝 |
| Handoff→Provision | Unit + Contract + Functional | handoff/provision/provider tests | Token+窗口+policy、稳定 operation ID、opaque relay、502/503 |
| Provider ownership | Architecture + Contract | boundary/provider tests | 无 backend 选择、Channel persistence/lifecycle/ingress、长期连接状态 |
| Device data exclusion | Architecture + Component | boundary/route tests | 无 Command/State/Event/Data/Media API、表或 Repository |
| Device Get/List | Unit + Contract + Component | query/device control/route tests | Owner scope、稳定 ID、filter/capability/q、有界 limit/cursor |
| Memory Directory | Unit + Component + E2E | memory/projection/restart tests | 从 Device 事实启动重建、DB-first、隐私、Owner scope |
| Device/Audit atomicity | Architecture + Unit + Component | mutation/projection/persistence tests | 单事务 commit/rollback、expected snapshot、event reuse rollback、retry repair projection |
| Management audit principal | Unit + Contract + Functional | JWT/lifecycle/schema/control-flow tests | 操作主体只来自 JWT `sub`、事件与幂等 fingerprint 绑定、payload 不可伪造 |
| Idempotency fingerprint | Unit + Component | enrollment/lifecycle/mutation tests | canonical JSON + SHA-256、delimiter collision、内容复用冲突 |
| Kernel owner namespace | Architecture + Kernel consumed contract | Hub precise Get + Kernel Hub adapter tests | Device→Owner Admission 与 Device→Companion Mount 单一权威、跨 Owner fail closed |
| Local-only Settings | Unit + Architecture | production config tests | 单 YAML、无 Profile/PostgreSQL/telemetry/Session、Secret 分离 |
| Current ORM | Unit + Architecture + Component | database/persistence tests | 精确两表、空库建表、旧/部分结构 fail-fast、不修改 |
| Single process | Unit + Architecture | runtime/boundary tests | SQLite 文件锁、第二进程 fail closed、无 fencing |
| Black-box contract | E2E | `test_local_contract_e2e.py` | Enrollment→approve→handoff→query/events→restart |

当前未完成且不能标记通过：真实小程序带外设备确认、真实固件和 `eidolon_channel` conformance、生产 TLS/mTLS、DNS/VLAN/mDNS 多接口环境、Provider/DB restart、网络分区、限流/DoS、Provider Revoke 残余 credential 窗口。
