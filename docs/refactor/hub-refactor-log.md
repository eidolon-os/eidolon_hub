# Hub 重构工程日志

本日志记录逻辑改动、首先失败的测试、所有权迁移、依赖变化、反思和风险。本轮尚未提交的条目以 `N/A (working tree)` 标记；不会虚构 SHA。

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
