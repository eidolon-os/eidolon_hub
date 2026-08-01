# Hub 重构工程日志

本日志记录逻辑改动、首先失败的测试、所有权迁移、依赖变化、反思和风险。当前工作区尚未创建提交，因此所有条目的 Commit SHA 为 `N/A (working tree)`；不会虚构 SHA。

## 2026-08-01 — Characterization baseline

- 目标：在改变入口前固定设备审批、注册、命令、Guard/Sense、blackboard 与 mDNS 的有价值行为。
- 修改前：Hub 的 register 返回 LiveKit token，LiveKit participant presence 参与在线状态，Router 可经 `app.state.data_store` 访问整个 DataStore。
- 首先失败：baseline 为 `196 passed, 1 failed`，失败是旧 mDNS TXT 仍断言 `config_url`；确认 Descriptor URI 才是新发现边界后更新 characterization expectation。
- 泄漏：Connection、Channel、Device Fact 在旧 DeviceManager 中混合；LiveKit URL/Room/Token 泄漏进 Hub 配置；DataStore 是隐藏 Service Locator。
- 测试：`python -m pytest -q`。
- Commit SHA：`N/A (working tree)`。

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

## 2026-08-01 — Provider-neutral Channel 与 DataEnvelope

- 目标：LiveKit 退出 Connection/online；Provider 动态产生 Channel Binding。
- 首先失败：opaque-binding 测试在旧 repr/响应中观察到 URL/token；provider mismatch 和 signaling failure tests 留下了孤儿 Channel Lease。
- 代码变化：Channel Profile/Catalog、Provision/Renew/Revoke Use Cases、NATS Provisioner Request/Reply、HTTP mailbox/MQTT Grant signaling、Channel lifecycle signals。
- 秘密策略：Hub 只验证 opaque bytes 长度并立即 relay；不解析、不持久化、不放入管理响应、不记录。
- Data Plane：外部 WSS/LiveKit Provider 通过标准 DataEnvelope NATS Bridge 接入；durable cursor 去重，Core 再校验 channel/device/TTL。MQTT Connector 没有 command/state/event send API。
- 功能验证：WSS-only provider command→Ack→Result；WAN MQTT registration contract→opaque realtime grant；普通与实时 Provider 共享 Orchestrator，不共享 Connection 判定。
- 反思：在 Hub 内做 MQTT↔LiveKit 协议转换会让 rendezvous transport 变成 data plane；标准 Provider Bridge 保持 Core 观察到一致 Envelope。
- Commit SHA：`N/A (working tree)`。

## 2026-08-01 — Directory、RBAC、可观测性与生产隔离

- 目标：durable 公共黑板与明确的生产 artifact。
- 首先失败：Owner transfer 测试显示旧 scope key 可残留；JWT tests 显示只验证 role 不能阻止跨 owner；生产配置检查发现旧 LiveKit/ESP32 字段仍在主模块。
- 代码变化：owner-scoped Directory CAS、atomic owner visibility transfer、management JWT、OTLP batch exporters 与秘密安全 HTTP middleware。
- 配置变化：生产 `hub/config.py` 仅含 Connection/Directory/logical Channel/OTLP；Provider URL/Room/Token/TURN/Codec 不存在。所有 config 在资源打开前校验。
- 删除：Characterization 阶段结束后，旧 register/token、LiveKit 在线判定、发送路径和 snapshot blackboard runtime 与对应旧测试依赖从 Hub 源码、dev dependency 和 wheel 中移除。
- 测试：production config invariants、package boundary AST tests、telemetry endpoint/redaction tests。
- 风险：现有兄弟项目 consumer 必须迁移到版本化 Schema 和 Provider binding；Hub 不提供旧合同兼容桥。
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
