# Hub 重构工程日志

本日志记录逻辑改动、首先失败的测试、所有权迁移、依赖变化、反思和风险。本轮尚未提交的条目以 `N/A (working tree)` 标记；不会虚构 SHA。

## 2026-08-04 — Hub 架构重构收尾：Device/Audit 原子性与 Kernel Owner Namespace

- 修改目标：关闭最后一个 Hub 内部一致性缺口，并把 Device→Owner Admission 与 Kernel Device→Companion Mount 明确收敛到同一 OS Owner Namespace、两个单一权威。
- 修改前行为：Enroll/Approve/Revoke 先独立提交 Device，再独立提交 Management Event，最后更新内存投影。设备提交后、事件/投影前失败时，幂等 request ID 已写入 Device，重试直接返回，无法补齐永久审计或当前进程投影。
- 首先失败：新增 `tests/component/test_atomic_device_mutations.py` 后 collection 因 `ConcurrentDeviceMutationError`/Mutation Port 尚不存在而失败；新增 JWT Principal 断言时 Authorizer 返回 `None`；最终 delimiter-collision 两个用例证明冒号拼接的 Approval/Revoke fingerprint 会把不同参数误判为相同操作。这三项均为预期 Red gate。
- 代码变化：新增窄 `DeviceMutationUnitOfWork.commit(expected, device, event)`；SQLite Adapter 在进程内异步写锁和一个事务内比较 expected immutable snapshot、写 `hub_devices`、写 `hub_events`。删除 Device Repository 非原子 `upsert` 和 Event Ledger 独立 Publish 入口。
- 恢复语义：事件校验、并发 expected 冲突或任意 DB 写失败会回滚 Device+Audit；提交后投影失败由同一 request ID 幂等重试重新投影。Revoke 的 Provider 调用仍处于事务外并使用稳定 operation ID 重试，不伪造跨服务数据库事务。
- OS 所有权：Owner 是 Kernel 根 Security/Namespace principal；Hub 只保存 Device→Owner Admission，不保存 Owner profile 或 `companion_id`。Kernel 回读 Hub Owner-scoped approved Device 并唯一拥有 Device→Companion Mount；Hub 不反向调用或导入 Kernel。
- 审计主体：新增不可变 `ManagementPrincipal`；JWT Authorizer 校验 `sub` 后返回 Principal，Router 将其 subject 绑定到 Approval/Revocation 事件和 request fingerprint。`owner_id` 继续表示 OS namespace，`principal_id` 只回答谁执行了管理操作，且不能由 payload 伪造；Enrollment 显式记为 untrusted device principal。
- 幂等语义：Enrollment/Approval/Revocation 不再使用换行或冒号拼接 fingerprint；统一将操作名和命名参数编码为 canonical JSON 后取 SHA-256，消除分隔符碰撞。
- Schema/依赖：SQLite ORM 仍为两表，但 `hub_events` 与 Management Event Wire Contract 新增 required `principal_id`。确认旧开发库为 0 Device/0 Event 后可恢复地备份到 `/private/tmp/eidolon-hub-db-backup.WzNKvw`，并按当前 ORM 直接重建；无 migration、兼容或新增运行时依赖。
- 测试：Architecture `26 passed`；Unit `70 passed`；Contract `15 passed`；Component `12 passed`；Functional `4 passed`；E2E `1 passed`；最终全量 `128 passed in 11.32s`、无 skip。Unit+Component+Functional 的 Domain/Application branch suite 为 `86 passed in 3.69s`、441 statements、114 branches、`97.30%`。Contract generation、Ruff、Import Linter 和 lock check 通过；clean wheel 构建通过，共 97 files，运行依赖不含 NATS、MQTT、LiveKit、SDK、Data、PostgreSQL 或 OpenTelemetry。
- 未证明：真实小程序 pairing/Enrollment Grant、密码学 Hub trust anchor、真实 Device/Provider/Companion Authority、复杂网络 Connectivity 与 Provider revoke 残余 credential 窗口。这些进入跨项目产品里程碑，不回流为 Hub 结构重构。
- ADR：新增 0019；同步更新 Normative 架构标尺和 OS 集成边界。
- Commit：本条目随对应代码提交交付；最终 SHA 以 `git log` 为准。

## 2026-08-04 — Hub 收敛为 Device Onboarding 与 Provider Handoff

- 修改目标：人工 Approval 后由 Provider 完全接管，Hub 不再重复维护长期设备连接、在线状态或 Channel lifecycle。
- 修改前行为：设备先完成 P-256 Challenge/Proof，Hub 持久化 Session、Heartbeat/Lease/Close，并以有效 Session 推导 Directory `online`；审批后通过 Register retry Provision。
- 首先失败：大范围删除后首次全量回归为 `119 passed, 2 failed`；失败分别是架构测试把已无源码的空 `sessions` 目录视为遗留，以及 Query fixture 的 kind 与过滤断言不一致。修正门禁为检查源码文件、修正 fixture 后全量通过。
- 领域变化：生命周期改为 `pending-approval / approved / revoked`；新增有界 Enrollment、retrieval token hash/window 和 Handoff；approved 明确不代表 online。
- 安全反思：retrieval token 只关联 Enrollment 创建方，不是物理设备认证。小程序必须通过二维码、短码、BLE/SoftAP 或物理确认完成带外识别，人工 Approval 才是授权边界。复查还发现 FastAPI 默认 422 会反射无效 Token 原文；Production App 现统一移除校验错误的 `input/url`，并由 Component 测试锁定。
- 代码/契约：删除 Challenge/Proof/Session/Heartbeat/Close/online/Directory Worker；Registration Schema 改为 Descriptor/Enrollment/Handoff；Provider Assignment 仍为请求级 opaque relay。
- Persistence：从 Device/Session/Challenge/Directory/Event 五表收敛为 `hub_devices` 与 `hub_events` 两表；内存 Directory 直接由设备事实重建，无 migration 或兼容。真实旧库再次确认 0 设备、0 事件后移动到 `/private/tmp/eidolon-hub-before-onboarding-handoff.sqlite3`，当前 ORM 已重建新库并确认 WAL。
- 配置/依赖：`device_access` 改为 `onboarding`；删除 Session Lease Secret、直接 `cryptography` 和旧配置；保留 Management JWT 与 Provider Token。
- 测试：Architecture `25 passed`；Unit `67 passed`；Contract `15 passed`；Component `9 passed`；Functional `4 passed`；E2E `1 passed`；最终全量 `121 passed in 4.28s`、无 skip。Domain/Application branch `97%`（432 statements、114 branches）。Contract generation、Ruff、Import Linter、lock check 全部通过；clean wheel 为 96 files，不含已删除 runtime。
- 未证明：真实小程序带外确认、真实设备/兄弟项目 conformance、生产 TLS/DNS/VLAN、Provider/DB restart、网络分区和 Provider credential 撤销窗口。
- ADR：新增 0018，修订 0013/0015/0016/0017 的长期 Session、online 和 Register retry 结论。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Characterization baseline

- 目标：在改变入口前固定设备审批、注册、命令、Guard/Sense、blackboard 与 mDNS 的有价值行为。
- 修改前：Hub 的 register 返回 LiveKit token，LiveKit participant presence 参与在线状态，Router 可经 `app.state.data_store` 访问整个 DataStore。
- 首先失败：baseline 为 `196 passed, 1 failed`，失败是旧 mDNS TXT 仍断言 `config_url`；确认 Descriptor URI 才是新发现边界后更新 characterization expectation。
- 泄漏：Connection、Channel、Device Fact 在旧 DeviceManager 中混合；LiveKit URL/Room/Token 泄漏进 Hub 配置；DataStore 是隐藏 Service Locator。
- 测试：`python -m pytest -q`。
- Commit SHA：`N/A (working tree)`。

## 2026-08-02 — 部署边界、严格 Profile 与版本化 Schema

- 修改目标：让同一 artifact 以最少选择逻辑运行 Local/Cloud，同时不把 Nginx/Ingress/Uvicorn 的内部监听参数混入设备公开契约。
- 修改前行为：`api.host/port` 同时控制 Uvicorn 与 mDNS SRV；`public_base_url` 宣称 HTTPS 但直接启动没有 TLS；即使 Cloud 已注入环境变量也强制要求 `.env` 文件；`enabled: true + empty endpoint` 会静默关闭 OTEL；启动以 `create_all()` 猜测 Schema；一个 reconciliation 周期同时驱动在线投影和 cache 全量刷新。
- 首先失败：无 `config/.env` 时直接 `HubConfig.load()` 得到 `FileNotFoundError`；首次在旧 `create_all()` PostgreSQL 测试库执行基线 migration 得到 `DuplicateTableError`，确认不能把无 revision 的旧开发 Schema 误当成当前 Schema 后，将测试迁到隔离临时 PostgreSQL Schema，而不是删除现有表或加入兼容接管。
- Settings：改为单一 strict/frozen/extra-forbid Pydantic Model；`EIDOLON_HUB_PROFILE=local|cloud` 选择两份完整配置，显式 YAML 路径为高级覆盖；删除 `api`、YAML OTLP endpoint、静态 shared instance ID、DSN env-name indirection、cache enable toggle 和重载 reconciliation 字段。
- 部署边界：ASGI host/port、TLS、forwarded-header trust 和反向代理归部署命令；Hub 只配置公开 HTTPS base。mDNS type 固定，实例名从 `hub_id` 派生，hostname/port 从公开 URL 派生。
- 运行时：Cloud instance ID 强制来自每副本唯一的 `EIDOLON_HUB_INSTANCE_ID`；dotenv 默认可选；OTEL 启用时必须存在标准 `OTEL_EXPORTER_OTLP_ENDPOINT`。
- Persistence：加入 packaged Alembic `0001`、幂等 upgrade 和 revision head check。Local 可启动迁移；Cloud 通过 `eidolon-hub-migrate` Job 升级，应用实例只验证 head。PostgreSQL credential-free DSN 在 YAML，user/password 在环境变量，并显式配置 pool timeout/recycle。Directory 始终 DB-authoritative + memory write-through；Local 不轮询，Cloud cache refresh 与 online projection 周期独立。
- 测试：默认 `144 passed, 2 skipped in 5.19s`；带本机 PostgreSQL 18 测试 DSN 全量 `146 passed in 5.25s`；Cloud `2 passed in 0.44s`；Unit `97 passed`；Architecture/Contract/Component/Deployment/E2E 全部通过；Domain/Application branch `98%`（`117 passed`）。
- 架构反思：公开地址和进程监听必须是两个概念；静默禁用 observability 会制造错误运维认知；数据库 revision 是 Cloud 多副本启动前置条件。当前 Cloud cache refresh 仍为全量扫描，大规模目录需要 watermark 增量读取。
- 未证明：真实 Nginx/Ingress TLS、OTEL Collector 可达性/认证、migration rolling deployment、DB failover 和网络分区仍是部署门禁。
- Commit SHA：`N/A (working tree)`。

## 2026-08-02 — 删除 Hub MQTT/Connection Connector，改为 Device Session + Direct Acquire

- 修改目标：WAN bootstrap 使用 Commissioned HTTPS Descriptor URI；mDNS 只做同链路 URI 发布；实际 MQTT 能力归外部 Channel Provider backend。Hub 注册、在线和通信通道不再混为 Connection Connector。
- 修改前行为：mDNS advertiser 与 MQTT client 被同一个 start/stop Port 包装；`ConnectionLease` 保存 connector kind、priority 与 signaling ref；后台 desired-state worker 通过 SQL claim 调 Provider sync，再经内存 mailbox/MQTT 投递 Grant。
- 首先失败：新增 `test_acquire_device_channels.py` 时因 Use Case 不存在而 collection failed；删除旧模块后 21 个测试文件因旧 Connection/MQTT/reconcile imports collection failed，随后逐类迁移而非保留兼容层。
- 契约变化：`connection/*` 改为 `session/*`；新增公共 `channel/assignment`、device acquisition 和 provider acquisition Schema；删除 MQTT AsyncAPI、signal delivery、旧 Channel Grant push Schema。生成 shapes 已重新生成，禁止手改。
- 实现变化：新增持久 `DeviceSessionLease/Authority`；设备注册获批后主动调用 `/api/device-access/v1/channels/acquire`；Hub 调 Provider `/device-channels/acquire` 并直接返回 opaque binding，只保存通用 lease metadata。命令与上行 Envelope 同时要求 active Session 和 active Channel。
- 删除代码：Hub MQTT adapter、Connection Port/Domain/Composition、Connector Supervisor、signaling mailbox/grant sender、desired-state reconciler/worker/Provider sync DB state，以及 Hub 端 Explicit URI/Unicast DNS resolver。
- 配置/依赖：`connection_plane` 改为 `device_access + discovery.mdns`；删除 MQTT setting/env、`aiomqtt`、`dnspython` 和无文件的 AsyncAPI package data。
- 测试迁移：删除 MQTT/Connector/reconcile 旧测试，重写 Session、HTTPS、Provider、Persistence、Local/Cloud parity 和黑盒 E2E；报告同步到 2026-08-02 当前契约。
- 验证：Architecture `15 passed`；Unit `84 passed`；Contract `13 passed`；Component `13 passed`；Local Functional `3 passed`；Deployment Mode `2 passed`；Contract E2E `1 passed`；本机 PostgreSQL 18 Cloud `2 passed`；带 PostgreSQL DSN 全量 `133 passed in 5.41s`。
- 覆盖率：Unit/Component/Functional 的 Domain + Application branch suite 为 `98%`（`795 statements / 234 branches`，`104 passed`），超过 `90%` 门禁。
- 架构反思：Connector 是伪抽象；进程内 mailbox 不能满足 Cloud 多实例；后台 Provider desired-state 把资源策略放错边界。Direct Acquire 把失败显式留在设备请求中，同时不回滚注册事实。Provider 资源清理由有限期 lease/credential 和 Provider 自己负责。
- 未证明：`eidolon_channel` 未修改；生产 TLS、DNS/VLAN、Provider/DB restart、网络分区和 rolling upgrade 仍需独立部署门禁。
- Commit SHA：`cd7e4b3`。

## 2026-08-01 — 分层骨架与 SDK 所有权迁移

- 目标：建立 `domain/application/ports/adapters/interfaces/composition`，Hub 不再直接依赖 `eidolon_sdk`。
- 首先失败：新增 AST 架构测试检测到 SDK imports、project dependency 和 uv source；Import Linter 在 Application 引入 Contract DTO 时失败。
- SDK 迁移：
  - Body Manifest/Capability → immutable `domain/devices/manifest.py` + JSON Schema；
  - Body Command/Control lifecycle → `domain/commands` state machine + Schema；
  - Blackboard semantics → durable owner-scoped Device Directory projection；
  - Device/Sense/Guard wire semantics → Hub Schema、generated raw Pydantic shape 与 validation tests；
  - Signature → `adapters/security/device_signature.py`，使用 `cryptography`；
  - Admin Client/SSE → Hub Port + injected `httpx.AsyncClient` / pure SSE helper；
  - LiveKit token builder 不迁入新生产路径。
- 代码变化：Wire DTO 与 Domain 通过 Mapper 转换；Application 的 `DataEnvelope` DTO 泄漏改成纯 Domain typed payload。
- 依赖变化：删除 project dependency `eidolon-sdk` 和 uv source，重新生成 `uv.lock`。后续 Hub 自有持久化阶段又移除了传递它的 `eidolon_data`。
- 验证：`lint-imports`、`tests/architecture`、`uv tree --depth 1 --no-dev`。
- 反思：复制 SDK package 结构会延续错误所有权，因此迁移的是语义和测试，不是目录。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — eidolon_data Port 化

- 目标：Core/Router 不看见 DataStore。
- 首先失败：Router service-locator 架构测试找到 `request.app.state.data_store`；真实 Adapter/Fake behavior tests 暴露 Device metadata 内的 request-id 字段需要显式映射。
- 代码变化：增加 Device、Command、Owner Context、Guard、Event Ledger、Asset Ports；`EidolonDataAdapters` 将未修改 DataStore 映射到独立 Adapter。
- 数据约束：没有修改兄弟项目源码或 schema；注册/管理 request ID 作为 Hub metadata 映射保存。
- 测试：`tests/component/test_persistence_adapters.py` 使用 Port 行为，Application tests 使用 Fake Repository。
- 反思：整个 DataStore 注入会使每个 Router 变成隐式 Composition Root；只注入 Use Case 后授权和事务边界可测试。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Connection Plane、发现与 authority

- 目标：多个 Connector 只负责 introduction/auth/register/lease/signaling。
- 首先失败：spoof tests 证明客户端可提交与 server binding 不同的 `connector_id`；多 Hub 测试证明相同 MQTT client ID 会互踢。
- 代码变化：
  - HTTPS 与 MQTT5 调用同一 Enroll/Register/Renew/Close Use Case；
  - MQTT Codec 使用 discriminator、Topic identity 校验和严格 operation allow-list；
  - `python-zeroconf` 多接口 IPv4/IPv6发布与动态刷新；
  - `dnspython` PTR/SRV/TXT/A/AAAA 单播查询；
  - 显式 HTTPS URI discovery 校验 commissioned URI；
  - durable Challenge、Connection、Device Authority CAS/fencing。
- 架构反思：RFC 8766 Discovery Proxy 不是标准 mDNS SDK 的透明扩展；URI 是资源受限或复杂网络设备最可靠的最低路径，SRP/Proxy 留给网络侧。
- 风险：Broker ACL 必须由部署同步配置，Hub allow-list 不能替代 Broker authorization。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Provider-neutral Channel 与 DataEnvelope（已被 ADR 0010 的 desired-state 模型取代）

- 目标：LiveKit 退出 Connection/online；Provider 动态产生 Channel Binding。
- 首先失败：opaque-binding 测试在旧 repr/响应中观察到 URL/token；provider mismatch 和 signaling failure tests 留下了孤儿 Channel Lease。
- 代码变化：Channel Profile/Catalog、Provision/Renew/Revoke Use Cases、NATS Provisioner Request/Reply、HTTP mailbox/MQTT Grant signaling、Channel lifecycle signals。
- 秘密策略：Hub 只验证 opaque bytes 长度并立即 relay；不解析、不持久化、不放入管理响应、不记录。
- Data Plane：外部 Provider 通过有方向、经认证的 HTTP DataEnvelope Bridge 接入；durable cursor 去重，Core 再校验 channel/device/TTL。MQTT Connector 没有 command/state/event send API。
- 功能验证：WSS-only provider command→Ack→Result；WAN MQTT registration contract→opaque realtime grant；普通与实时 Provider 共享 Orchestrator，不共享 Connection 判定。
- 反思：在 Hub 内做 MQTT↔LiveKit 协议转换会让 rendezvous transport 变成 data plane；标准 Provider Bridge 保持 Core 观察到一致 Envelope。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Directory、RBAC、可观测性与生产隔离

- 目标：durable 公共黑板与明确的生产 artifact。
- 首先失败：Owner transfer 测试显示旧 scope key 可残留；JWT tests 显示只验证 role 不能阻止跨 owner；生产配置检查发现旧 LiveKit/ESP32 字段仍在主模块。
- 代码变化：owner-scoped Directory CAS、atomic owner visibility transfer、management JWT、OTLP batch exporters 与秘密安全 HTTP middleware。
- 配置变化：生产 `hub/config.py` 仅含 Connection/Directory/logical Channel/OTLP；Hub 只知道 Provider control endpoint，不知道设备侧 URL/Room/Token/TURN/Codec。所有 config 在资源打开前校验。
- 删除：Characterization 阶段结束后，旧 register/token、LiveKit 在线判定、发送路径和 snapshot blackboard runtime 与对应旧测试依赖从 Hub 源码、dev dependency 和 wheel 中移除。
- 测试：production config invariants、package boundary AST tests、telemetry endpoint/redaction tests。
- 风险：现有兄弟项目 consumer 必须迁移到版本化 Schema 和 Provider binding；Hub 不提供旧合同兼容桥。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — 配置契约收敛

- 目标：让文件布局、Setting Model 与 Composition 的真实读取完全一致，删除旧 LiveKit 和静默无效字段。
- 代码核查：`logging.level`、`unicast_dns_sd_service`、`explicit_descriptor_uris` 无生产消费者；旧根 `.env` 与 `config/.env` 只含 LiveKit/旧 mDNS/Admin 字段，当前 Composition 不读取。
- 结构变化：mDNS 移入 `connection_plane.mdns`，但类型命名为 `MdnsDiscoveryConfig`，只发布 Descriptor URI、不产生 `ConnectionLease`；MQTT 仍是实际 WAN Connection Connector。
- 删除：重复 `settings.example.yaml`、根 `.env.example`、未使用 logging helper 与 `pydantic-settings`；本地旧 `.env` 文件已删除，统一从 `config/.env` 加载。
- 防回归：YAML root 与每个固定 section 使用 unknown-key fail-closed；旧 top-level `mdns`、`logging` 和显式 URI Hub 配置会直接拒绝，不再静默忽略。
- Channel 边界：没有具体媒体 Setting；后续 ADR 0010 进一步删除了 Hub-owned Profile，只保留 Provider contract URL。
- 安全收敛：布尔 Setting 只接受原生 YAML boolean；示例密钥为空并令启动 fail-closed，避免公共占位符被当作有效凭据。
- 当前结果：`134 passed`（含严格布尔与废旧配置防回归用例）。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — 当前验收闭环

- `python -m pytest -q`：`123 passed in 4.17s`；临时 Characterization Tests 已在对应语义迁入新分层测试后删除。
- `python -m pytest tests/unit tests/component tests/functional --cov=hub.application --cov-branch --cov-fail-under=90`：`Application 100%`。
- Domain + Application branch suite：`922 statements / 222 branches / 100%`；Channel/Connection/Device/Manifest/Command 的 constructor 与状态分支全部覆盖。
- `python scripts/generate_contracts.py --check`：generated tree 可复现。
- `ruff check hub tests scripts`、`ruff format --check hub tests scripts`、`lint-imports`：全部通过，3 条 import contract 均 kept。
- `uv tree --frozen --depth 1 --no-dev`：通过；Hub direct dependency 中没有 `eidolon-sdk` 或 LiveKit。
- `uv build --wheel`：成功；发布 wheel 包含 JSON Schema、AsyncAPI、golden examples 和生成 DTO，不含旧 runtime；METADATA 无直接 `eidolon-sdk`/LiveKit dependency。
- 下一步门禁：任何失败都先记录原因和边界影响，再修改实现；不通过测试不得标记阶段完成。

## 2026-08-01 — Hub 自有数据库、内存热投影与 NATS 完整剔除

- 目标：Hub 作为契约化 Device Bus 独立运行，不再要求 NATS/JetStream 或 `eidolon_data`。
- 修改前行为：Composition 无条件打开 DataStore、NATS client 与 JetStream KV；Challenge/Connection/Authority/Directory/Channel/Cursor 分散在 KV，事件和 Provider data 又依赖 NATS subject。
- 代码核查：Hub 自定义 provision/data/event subjects 没有兄弟项目 responder/consumer；Agent 读取的旧 KV bucket 使用另一组 key/value contract，与新 Directory 不兼容。保留 NATS 不会得到真实集成能力。
- 首先失败：架构测试在 project metadata、Composition 与 Adapter 找到 `nats-py`/`eidolon-data`；生产配置测试仍要求 subject/bucket。
- 持久化变化：新增 Hub 自有 `hub_devices`、`hub_commands`、Challenge、Connection/Authority Lease、Directory、Channel Lease/Cursor 与 Event schema；SQLite 使用 WAL，PostgreSQL 使用 asyncpg pool。所有实现位于 Persistence Adapter。
- 热路径：Device Directory 启动 hydrate、DB-first write-through、内存读取和周期性跨实例 reconcile。认证、授权、吊销、命令、fencing 与 cursor 不走软缓存。
- 传输变化：Provisioner 与 DataEnvelope bridge 改成 bearer-authenticated HTTP；MQTT AsyncAPI 删除业务 DataEnvelope channel，仍只允许 Connection/Channel signaling。
- 删除：NATS client/event bus/JetStream KV Adapter、全部 KV Repository、`EidolonDataAdapters`，以及 `nats-py`、`eidolon-data` uv source/dependency。
- 依赖结果：`uv tree --depth 1` 无 NATS/Data/SDK/LiveKit；`uv.lock` 同时移除了 `eidolon-sdk` 和 LiveKit 的传递包。
- 测试：Hub SQLite 完整 Composition 生命周期、全部 SQL Repository round-trip/fencing/single-use/idempotency、内存 DB-first 失败语义、跨实例 refresh、HTTP Provider auth/contract。
- 当前结果：`128 passed`；Application branch coverage `100%`；Ruff、Import Linter、wheel build 与 deterministic contract generation 均通过。
- 反思：memory-first + async DB write 会在崩溃时丢失审批/命令/租约，且 Cloud 多实例无法把单机内存当权威源。因此只缓存可容许短暂陈旧的公开 Directory projection。
- 风险：兄弟项目尚未实现新的 Provider HTTP 与 Hub API consumer；尤其 Agent 旧 NATS KV reader 必须独立迁移，不能把 Hub NATS 删除误写成整栈 E2E 已完成。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Channel Provider 契约透传、稳定性闭环与死代码删除

- 目标：Hub 不配置 Profile/Provider 路由，只配置一个契约基址；设备事实持久化后异步把 typed context 交给 Provider，由 Provider 返回 generic Assignment + opaque binding。
- 首先失败：新 Provider reconcile/contract tests 在 collection 时缺少 Application/Adapter；lifecycle tests 暴露旧 Mapper；黑盒 E2E 因 ambient HTTP proxy 把 localhost Provider 调用导向 `127.0.0.1:7890` 而得到 502。
- 第二轮反思失败：Unit Fake 可在 30 秒后重新 claim pending Grant，但真实 SQL Repository 把相同 succeeded desired revision 直接拒绝；新增 Component test 首先失败。临近过期 active/pending lease 也没有新 issuance generation，且过期 Provider 响应可通过验证；三个新测试首先失败后推动修复。
- Contract：新增 Provider sync 与 lifecycle JSON Schema/DTO，固定 `/device-channels/sync`、`/data/envelopes`、Hub lifecycle/data ingress；Channel Grant 只含 purpose/kinds/binding format/lease/opaque binding。
- Application：desired revision 表示设备事实；失败重试和仍可用 pending 补投复用 operation；终态或进入 30 秒刷新窗口时，根据通用 lease generation 产生新 operation。SQL claim 只负责进行中互斥。
- 安全：Provider egress 使用 `httpx.AsyncClient(trust_env=False)`；opaque binding 只在 Adapter 做 base64 编解码和中继，不进入 SQL、Directory、Event Bus 或日志。
- 删除：Profile/Provision/Renew/Revoke/设备 Channel negotiation、旧 Provisioner client 与对应 Schema/tests；同时按生产 import、Composition 和所有权删除无消费者 Admin/SSE/旧 signature/access/assets/owner-context/capability/logging/空 Messaging/EventLedger 实现。Guard/Sense 发布契约保留。
- Directory 反思：Cache 周期 reload 只会复制持久化投影，无法让没有 Disconnect 的自然过期 Connection 变为 offline。新增 Application `execute_all` projection 与独立调度 Adapter；SQL 忽略纯投影时间变化，避免无变化时 revision churn。
- Cloud 解阻：直连 `18.4 (Postgres.app)`，创建隔离 `eidolon_hub_test`；Homebrew Mosquitto 使用 `127.0.0.1:1883`。新增两个独立 engine/pool 的真实并发 Channel claim 测试。
- 测试：Unit `90 passed`；Contract `25 passed`；Component `14 passed`；Local Functional `6 passed`；Cloud Infrastructure `3 passed`；Deployment parity `2 passed`；真实 TCP Local Contract E2E `1 passed`；Architecture `15 passed`；注入本机基础设施后全量 `156 passed`。Domain/Application branch coverage `95%`，全 Hub `77%`。
- 未执行：生产 MQTT TLS/ACL、PostgreSQL/Broker/Provider restart、网络分区与 rolling restart；本机明文 Mosquitto 通过不等于生产 Cloud E2E。
- 发布反思：首次 wheel 因复用历史 `build/lib` 仍包含已删文件；将精确生成目录移到 `/private/tmp` 后，最新 clean rebuild 得到 `135` entries，owner contract 与 remote HTTPS guard 已打包，旧实现文件和 NATS/Data/SDK/LiveKit dependency matches 均为 0。
- 风险：外部 `eidolon_channel`/Device/Agent 尚未实现新契约；真实 Broker ACL/TLS、PostgreSQL 多实例、Provider/Broker/DB restart、VLAN/DNS 和 rolling restart 仍需部署环境门禁。
- 证据：`docs/testing/reports/`、`architecture-findings.md`、`code-inventory.md`。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Composition Root 瘦身与 Runtime Adapter 归属修正

- 目标：让 `hub/application` 继续只表达业务编排；让 `hub/composition` 只负责选择、组装和生命周期，不持有 Clock/ID 基础设施实现，也不以单个 395 行函数承载全部子系统 wiring。
- 修改前行为：`composition/app.py` 同时创建数据库、缓存、HTTP Client、Connection Connector、Channel Bridge、管理服务和全部 Use Case；`SystemClock`、`SecureIdGenerator` 位于 Composition。业务行为已有测试，但模块所有权和失败清理边界不够清楚。
- 刻画与首先失败：修改前先执行 Composition/Deployment/E2E 测试，`6 passed`；拆分后相同测试仍为 `6 passed`。第一个失败门禁是 Ruff `I001` 导入顺序，不是业务回归，修正后通过。
- 代码变化：Clock/ID 移到 `hub/adapters/runtime.py`；新增 resources、connection plane、channel control、device management 四个显式装配模块；唯一 `create_composed_app` 入口由 395 行降至 138 行。`ApplicationHttpRuntime` 更名为准确的 `ComposedHttpRuntime`。
- 生命周期修正：单一 `AsyncExitStack` 现在覆盖完整装配过程；任一工厂、Connector 或 Worker 启动失败都会关闭已打开的数据库、Directory cache、HTTP Client、telemetry 和已启动组件，不再只处理资源打开或 Supervisor 启动两个局部阶段。
- 边界复核：Application/Domain/Ports 没有新增 Adapter、Interface 或第三方 import；Channel Provider 契约、opaque binding 中继、注册和 Directory 语义没有改变。
- 测试：Runtime Adapter Unit `2 passed`；Unit `92 passed`；Architecture `15 passed`；Import Linter `108 files / 179 dependencies / 3 contracts kept`；原 Composition/Deployment/E2E `6 passed`；本机 PostgreSQL 18/Mosquitto Cloud Infrastructure `3 passed`；注入基础设施的全量回归 `158 passed in 10.77s`、无 skip；全 Hub branch coverage `78%`。
- 架构反思：把装配逻辑拆成工厂并不会创建多个 Composition Root；具体实现仍只在 `create_composed_app` 所有的生命周期内被选择一次。后续不应为了减少文件行数继续抽象，只有子系统依赖图或测试边界发生变化时才增加新的装配结构。
- 风险：本次没有扩大生产基础设施证据；MQTT TLS/ACL、Broker/DB/Provider restart、网络分区和 rolling restart 仍是部署环境门禁。
- Commit SHA：`N/A (working tree)`。

## 2026-08-02 — Hub 收敛为纯 Device Manager

- 修改目标：Hub 不再承担 Device Bus/data plane；全部 Command/State/Event/Data/Audio/Video 归属外部 Channel Provider。Hub 只保留设备管理和低频 Provision/Revoke/Lifecycle control。
- 修改前行为：设备审批后调用独立 `/channels/acquire`；Hub 保存 Command Ledger，通过 Provider Data Bridge 双向中转 DataEnvelope，并用 Channel Cursor 做顺序/去重。
- 首先失败与推动：删除实现后，旧 Unit/Functional/E2E 在 collection 直接引用 Command/DataEnvelope；这些测试不是兼容需求，而是错误所有权的证据，因此按新边界删除或重写。Provider 异常测试进一步暴露非法响应、设备幂等冲突都映射成 409，新增 `ChannelProviderContractError` 后分别映射 502/409，不可达保持 503。
- Domain/Application：删除 commands domain、Send/Get/Ingest use case；`ProvisionDeviceChannels` 只验证通用对应关系/时间/状态，不要求 management purpose 或 kind。`RegisterDeviceAndProvision` 实现 pending approval 与审批后 Register retry。
- Contract/API：删除 Channel Acquisition、Device Command、DataEnvelope Schema/DTO/example；注册响应新增 typed `DeviceRegistrationOutcome`；Provider 固定 Provision/Revoke，Gateway 只保留 Lifecycle。管理事件由 `DeviceBusEvent` 更名为 `DeviceManagementEvent` 并补充 JSON Schema。
- Persistence：删除 `hub_commands`、`hub_channel_cursors`、对应 SQL Model/Repository/初始迁移；只保存 Channel Assignment metadata，opaque binding 仍不可持久化。
- 安全状态机：首次注册返回 `approved=false, channels=[]`；未审批不向 Provider 请求可用 binding。审批后设备用新 request ID 在有效 Session 上重试注册，binding 只在该认证响应中转交。吊销先持久化 fail-closed 并关闭 Session/Channel metadata，再调用 Provider；Provider outage 时相同 operation ID 可重试。
- 删除：Data Bridge、Command/Cursor runtime 与全部专用测试；无遗留兼容 Router、Schema 或表。
- 分层测试：Unit `77 passed`；Architecture `16 passed`；Contract `12 passed`；Component `13 passed`；Local Functional `3 passed`；Deployment parity `2 passed`；TCP Contract E2E `1 passed`。
- 覆盖率：Domain + Application branch suite `96.22%`（629 statements、192 branches），超过 90% 门禁。
- 全量回归：默认 `124 passed, 2 skipped in 4.49s`；注入本机 PostgreSQL 18 DSN 后最终 `126 passed in 4.59s`，无 skip。Cloud isolation tests `2 passed in 0.47s`。
- 工具门禁：Contract generation check、Ruff、Import Linter 和 dead-reference audit 纳入最终验收。
- 架构反思：Hub↔Provider 是低频控制面，没有代码证据支持 gRPC stream；HTTP JSON 保持最小复杂度且可在 Adapter 替换。当前同步 revocation 没有 durable outbox，Provider 不可达期间旧 credential 的残余窗口必须由 Provider lease 上限约束，并留给后续故障注入验证，不能臆造为已解决。
- ADR：新增 0013，取代 ADR 0005/0008 的 Device Bus/DataEnvelope 结论和 ADR 0011 的独立 Acquire 流程。
- Commit SHA：`N/A (working tree)`。

## 2026-08-03 — 删除内置 OpenTelemetry 集成

- 修改目标：当前未确定 telemetry 平台和运行要求，不在 Hub 内提前集成第三方 Observability runtime。
- 修改前行为：Composition 启动 OTLP/gRPC Trace、Metric、Log exporter 和 HTTP middleware；Cloud 配置必须提供 OTLP endpoint，并直接依赖 OpenTelemetry SDK/exporter、gRPC 和 protobuf。
- 代码与配置：删除 `adapters/observability`、middleware 和 lifecycle；删除 Settings/YAML/环境变量；不添加 no-op Port。Hub 只保留 Python/Uvicorn 标准输出日志。
- 依赖结果：`uv lock` 移除 OpenTelemetry 全族、`grpcio`、`protobuf` 和 `googleapis-common-protos`；`uv tree --frozen --depth 1 --no-dev` 只保留 Hub 实际运行依赖。
- 防回归：Architecture 门禁断言 Observability 目录不存在、Settings 不含该字段、环境示例无 OTEL 变量且 Package Metadata 无 OpenTelemetry 直接依赖。
- 测试：Unit `73 passed`；Architecture `17 passed`；Contract `12 passed`；Component `12 passed`；Functional `5 passed, 2 skipped`；E2E `1 passed`。默认全量 `120 passed, 2 skipped in 5.68s`；注入本机 PostgreSQL 18 DSN 后 `122 passed in 5.27s`，无 skip。
- 覆盖率：Domain + Application branch `96.34%`（627 statements、192 branches），超过 90% 门禁。Contract generation、Ruff、Import Linter 和依赖锁检查均通过。
- 架构反思：没有真实 consumer 和服务等级目标时，telemetry abstraction 只会增加虚假的可替换性。未来必须先明确平台、信号、采样、脱敏、失败语义和验收，再通过独立 ADR 重新引入。
- ADR：0014 取代 ADR 0012 的内置 OpenTelemetry 条目。
- Commit SHA：`N/A (working tree)`。

## 2026-08-03 — 运行形态收敛为 Local-only 单进程 SQLite

- 修改目标：保留 Contract/Domain/Application/Port/Adapter/Composition 分层，删除尚无真实产品证据的 Cloud/PostgreSQL/多实例分支，避免 Eidolon OS 所有兄弟项目被双模式复杂度绑架。
- 修改前行为：`EIDOLON_HUB_PROFILE` 选择 Local/Cloud YAML；Persistence 同时支持 SQLite 与 asyncpg/PostgreSQL；Cloud 启动只检查 migration head；`DeviceAuthorityLease`、instance ID 和 fencing token 处理多实例设备竞争；Directory 周期全量 refresh 观察其他实例写入。
- 首先失败：新增 Local-only Architecture characterization 后，测试首先因缺少单一 `config/settings.yaml`、Package Metadata 仍含 `asyncpg` 而失败，证明删除目标可被自动化观察；随后才修改生产代码。
- 配置：合并为唯一严格 `config/settings.yaml`；删除 `settings.local.yaml`、`settings.cloud.yaml`、Profile/instance/PostgreSQL 环境变量。`.env` 只保存 Lease、Management JWT 和 Provider Token 三个 Secret。
- Persistence：Composition 只创建 Hub 独占 SQLite，默认 `/Users/manson/eidolon/data/eidolon-hub.sqlite3`，启动执行 packaged Alembic migration；删除 PostgreSQL factory/pool、asyncpg、独立 migration CLI 和 Cloud Functional/Parity tests。初始迁移只保留六张当前业务表。
- 单进程所有权：新增 `LocalProcessLock`，打开数据库前非阻塞独占 `<database>.lock`；第二个 Hub 进程共享同一路径时 fail closed。锁文件存在不等于锁被持有，正常退出释放内核锁。
- Domain/Application：删除 Authority Entity/Repository、`hub_instance_id`、`fencing_token`、authority acquire/renew 和按最大 fence 过滤 Session；在线只由本进程持久 active Session 与 expiry 推导。
- 热路径：Device Directory 仍为 SQLite authoritative + in-memory write-through，启动 hydrate、DB-first 更新；删除跨实例 background refresh，保留显式 refresh 与时间派生投影周期。
- 身份边界：`tenant_id` 保留为本地逻辑 Realm。Cloud 删除不自动授权改变设备身份 Wire Contract；是否移除需另立契约决策。
- 发布门禁：首次增量 wheel 构建暴露历史 `build/lib` 会重新打包已删 Data Bridge、Command、Acquire、Observability、旧 Schema 和 migration CLI。删除精确生成目录并 clean rebuild 后，wheel 为 `110 files`，只含单一 `config/settings.yaml`；METADATA 无 asyncpg/PostgreSQL。新增 Architecture 测试禁止现存 build cache 含有源码树已删除文件。
- 测试：Architecture `19 passed`；Unit `68 passed`；Contract `12 passed`；Component `11 passed`；Local Functional `3 passed`；Contract E2E `1 passed`；全量 `114 passed in 4.48s`，无 skip。Domain + Application branch coverage `96.19%`（604 statements、184 branches）。
- 架构反思：Repository Port 的价值是上层所有权隔离、Fake 行为测试和避免 SQL 泄漏，不代表当前承诺多数据库。若未来出现真实 HA/远程需求，必须先定义容量、故障模型、一致性和部署验收，再以新 ADR 重新设计，不能只恢复一个 Profile 分支。
- ADR：新增 0015，取代 0006，并取代 0009/0012 中 PostgreSQL、Local/Cloud Profile、多实例和 fencing 条目。
- Commit SHA：`N/A (working tree)`。

## 2026-08-03 — Device Control Subsystem 状态、存储与查询契约收敛

- 修改目标：以 Device Control Subsystem 标尺统一设备生命周期、Local 持久化和 OS 项目查询方式，避免把认证、在线、审批和 Channel 可用性塞进万能状态机。
- 首先失败：新增 Contract test 在 collection 时缺少 `SessionCloseRequest`；Wire Identity 仍接受设备提交的 `tenant_id`；Directory 仍暴露 Session ID 和二次编码 Manifest。新增 Route/Query tests 同时冻结 Owner-scoped Get/List 和旧路径删除。
- 状态模型：持久设备生命周期只保留 `pending-approval -> active -> revoked`，revoked 为终态；认证 Session、由有效 Lease 派生的 online 和 Provider Channel lifecycle 分开建模。删除 `approved + revoked` 双布尔组合。
- 身份和 Provider 边界：删除设备可提交的 Authority Scope；Provider Provision 只传 device/hub/operation、fingerprint、Owner、display/kind、Manifest/revision。active + authenticated 已由调用资格表达，不再重复发送 approved/revoked/connected。
- Persistence：SQLite 继续是 Local 单进程权威源，Device Directory 是 DB-first、in-memory write-through 热投影。新增 Alembic 0002，将 0001 的 tenant/双布尔无损转换为 lifecycle，并将旧 Directory JSON 转成 typed Manifest、Session count/expiry，删除公共 Session ID；真实配置库已从 0001 升到 0002，迁移前后均为 0 个设备。
- Query/Application：新增 `GetDevice` 和 `ListDevices`。List 支持 lifecycle、online、kind、capability 和可选 UI `q`，所有输入有界，limit 为 1..100，结果按稳定 device ID cursor 分页。Router 只依赖 Query，不直接访问 Repository。
- Transport 决策：不增加 Search RPC，不引入 gRPC/Protobuf。当前兄弟项目证据是 Admin Owner 列表/详情、Agent 能力过滤和 Channel 精确 ID 解析，均为低频控制查询；HTTP/JSON 保持 JSON Schema/OpenAPI 单一契约源并直接兼容小程序、浏览器和 Python 服务。
- Contract：新增 Directory Page、Management Event 和公共 lifecycle Schema；Manifest 在 Directory 中为 typed object；Session Close request/response 分离且响应不回显 Lease token；内部持久事件改名为 Device Management Event Ledger，避免暗示系统消息总线。
- 架构标尺：新增 `docs/architecture/device-control-subsystem.md` 和 ADR 0016；总览文档删除 Authenticated/Approved/Provisioned 混合状态机。
- 测试：Architecture `21 passed`；Unit `77 passed`；Contract `16 passed`；Component `11 passed`；Local Functional `3 passed`；Contract E2E `1 passed`；全量 `129 passed in 4.55s`，无 skip。Domain + Application branch coverage `95%`（670 statements、208 branches）。Contract generation、Ruff、Import Linter、clean wheel build 与 stale-file audit 均通过；wheel `118` entries，无 retired runtime 或 gRPC/NATS/MQTT/LiveKit dependency。
- 架构反思：SQLite 适合当前设备规模、单进程写入者和本地故障域；内存索引优先于提前引入远程数据库。只有真实容量、HA 或远程共享需求越过该边界时，才以新 ADR 重选存储。Transport 也只应在 profiling 证明 HTTP 控制查询成为瓶颈后替换 Adapter。
- Commit SHA：`N/A (working tree)`。

## 2026-08-03 — 删除数据库迁移与开发期兼容层

- 修改目标：项目仍在开发期，不维护 SQLite 历史结构。当前 ORM Model 是唯一 Schema，删除 migration、downgrade、旧列/旧 Directory 转换和 revision 元数据。
- 首先失败：新增 Architecture/Database tests 首先因仍有 Alembic dependency、migrations 目录以及缺少 `initialize_schema` 而失败；旧 Schema 测试明确要求“不修改后拒绝”，防止兼容逻辑换名回归。
- 运行时：`HubDatabase.initialize_schema` 在空库直接按 `Base.metadata.create_all` 建表；非空库严格比较业务表和列集合，任何缺失或额外结构都 fail-fast 并要求删除独占数据库后重建。不存在自动 ALTER、数据转换或隐式接管。
- 删除：整个 `hub/adapters/persistence/migrations`、0001/0002、Alembic env/template、迁移/降级 API 和兼容测试；`pyproject.toml`/`uv.lock` 移除 Alembic 与 Mako，package data 不再包含 migration 文件。
- 配置库：真实数据库业务表已是当前结构且设备数为 0；仅删除废弃的 `alembic_version` 表，再由新初始化器成功严格校验。现在数据库只包含六张当前 ORM 业务表。
- 测试：Architecture `22 passed`；Unit `76 passed`；Contract `16 passed`；Component `11 passed`；Local Functional `3 passed`；Contract E2E `1 passed`；最终全量为 `129 passed in 4.21s`，无 skip。Domain + Application branch coverage `95%`（670 statements、208 branches）。Clean wheel 为 `112` entries，不含 migration 文件、Alembic/Mako dependency 或旧 revision。
- 架构反思：Repository Port 负责隔离持久化细节，不等于必须维护历史 Schema。开发期 fail-fast 重建比未经产品授权的数据迁移更清晰；进入需要保留用户数据的发布阶段前，必须重新决策 Schema 生命周期，而不能继续依赖 `create_all` 处理演进。
- Commit SHA：`N/A (working tree)`。

## 2026-08-03 — `hub/` 逐文件死代码审计与 Channel 状态所有权收口

- 修改目标：逐一阅读 `hub/` 的手写模块、Schema、生成 shape 和 example，以“Hub 只做跨会话 Device Manager”为删除标尺，不保留开发期兼容或只被旧测试托住的实现。
- 首先失败：先把测试改为无 Provider ingress、无 Channel Repository、Assignment 仅请求级透传；目标 Unit/Component 首次得到 `12 failed, 2 passed`，失败点正是旧构造参数与 Lifecycle route。
- 关键发现：`hub_channel_assignments` 只有 Provision/Lifecycle 写入路径；Directory、在线、管理查询、授权、事件流和 Revoke 都不读取。Hub 因此无收益地复制了 Provider 状态机。
- 整链删除：`ChannelLease/ChannelState/ChannelLifecycle`、Repository/SQL Model/第六张表、Record Use Case、Lifecycle Schema/generated shape/Mapper、Provider Gateway Router、Composition 入站服务和三组专用旧测试。保留唯一 outbound Provider Provision/Revoke Port 与请求级 Assignment 校验/opaque relay。
- 局部删减：Enrollment 不再返回 Router 立即丢弃的 fingerprint；Session Entity 不再提供无人使用的隐式系统时钟或重复 credential 比较参数；Heartbeat/Close 不再把高频 Session traffic 写入永久管理审计。
- 存储反思：Session/Challenge 的 `version` 从未参与条件更新，SQLite `SELECT FOR UPDATE` 又不提供行锁，属于虚假并发保障；Directory 的 updated/revision 列完整重复 typed payload 且不用于查询。删除这些字段后，Challenge consume 使用 `consumed=false` 条件原子更新，Directory 本地写入在 memory-hot write-through Adapter 中串行化。
- 数据库：配置库原六表均为 0 行；旧空库移动到 `/private/tmp/eidolon-hub-before-dead-code-prune.sqlite3`，当前 ORM 已重建五张表，Challenge/Session/Directory 均不含废弃字段。
- 逐文件证据：`docs/testing/code-inventory.md` 为全部手写生产模块给出处置理由，并逐对登记 15 个 Schema source 与 generated shape；剩余每个模块均有真实装配/调用或明确边界责任。
- 测试：Architecture `24 passed`；Unit `71 passed`；Contract `14 passed`；Component `12 passed`；Local Functional `3 passed`；Contract E2E `1 passed`；全量 `125 passed in 3.48s`，无 skip。Domain/Application branch suite `87 passed`，579 statements、166 branches，覆盖 `97%`。
- 工具与发布：Contract generation freshness、Ruff、Import Linter（84 files / 127 dependencies / 3 contracts kept）通过；clean wheel `108 files`，不包含 Provider Gateway、Channel lifecycle/persistence、旧 data/command/migration runtime。
- ADR：新增 0017，明确 Assignment 是请求级透传而非 Hub 状态；更新 0010/0011/0013/0016 的取代关系和当前结论。
- 恢复性：本轮源码删除仍可由 Git 恢复；旧空数据库备份在 `/private/tmp`，缓存与构建产物可重新生成。
- Commit SHA：`N/A (working tree)`。
