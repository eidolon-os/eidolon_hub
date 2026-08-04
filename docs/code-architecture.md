# Eidolon Hub 代码架构与完整导览

- 日期：2026-08-04
- 范围：`hub/` 当前保留的全部 Python、Schema 和 Example

## 1. 启动与请求路径

```text
hub.main:app
  -> composition.app.create_composed_app
  -> resources: process lock + SQLite + read repositories + atomic mutations + memory directory + HTTP client
  -> hydrate directory from hub_devices
  -> device_onboarding / management / channel_control composition
  -> onboarding and management routers
```

设备路径是 `Descriptor -> Enrollment -> Human Approval -> Handoff -> Provider`。Approval 后，管理端可独立调用 Kernel Mount；Kernel 通过 Owner-scoped Get 回读 Hub 准入事实。Handoff 后设备不再访问 Hub；没有 Session、Heartbeat 或 data plane。

## 2. 依赖方向

```text
domain
ports       -> domain
application -> ports + domain
contracts   -> ports + domain
adapters    -> contracts + ports + domain
interfaces  -> contracts + application + ports + domain
composition -> interfaces + adapters + contracts + application + ports
main        -> composition
```

`__init__.py` 都只是 package marker。以下逐一解释所有有运行逻辑的 Python 文件。

## 3. 顶层与 Domain

| 文件 | 作用 |
|---|---|
| `hub/main.py` | ASGI 稳定入口，导出 `create_app` 和 `app`。 |
| `hub/config.py` | 严格 Local-only Settings Model，读取单一 YAML 与可选 `.env`，拒绝未知字段。 |
| `domain/devices/identity.py` | 不可变稳定 `DeviceIdentity`，只有 Device ID。 |
| `domain/devices/manifest.py` | 不可变 Manifest Document、revision 和能力查询。 |
| `domain/devices/entities.py` | Enrollment intent、Managed Device、三态 lifecycle 与公共 Directory Entry。 |
| `domain/channels/entities.py` | Provider-neutral Provision/Revoke context、Assignment、opaque binding 及不变量。 |

Domain 不含 HTTP、ORM、Pydantic、配置或 Provider backend 类型。

## 4. Application

| 文件 | 作用 |
|---|---|
| `application/idempotency.py` | 将操作名与命名参数编码为 canonical JSON，并生成无分隔符碰撞的 SHA-256 fingerprint。 |
| `application/use_cases/enroll_device.py` | 幂等创建有界 Enrollment，以一个 Mutation 原子保存 Token hash、pending 设备和管理事件，再投影。 |
| `application/use_cases/handoff_device.py` | 校验 Enrollment、Token、窗口和策略；approved 时调用 Provision。 |
| `application/use_cases/provision_device_channels.py` | 建立最小受信 Provider context，校验返回关联性/有效期，返回请求级 Assignment。 |
| `application/use_cases/approve_device.py` | 人工批准、绑定 Owner，以一个 Mutation 原子打开有限 Handoff 窗口并审计。 |
| `application/use_cases/revoke_device.py` | 以一个 Mutation 原子终态吊销并审计，投影后幂等通知 Provider Revoke。 |
| `application/queries/get_device.py` | 从内存 Directory 按稳定 ID读取并强制 Owner scope。 |
| `application/queries/list_devices.py` | Owner-scoped 结构化过滤、搜索、稳定 cursor 和有界分页。 |
| `application/projections/device_directory.py` | 由权威 Device 事实生成安全 Directory Entry，并支持启动全量重建。 |

Application 只依赖 Domain 与 Port，不知道 HTTP status、SQLite 或 Provider transport。

## 5. Ports

| 文件 | 接口责任 |
|---|---|
| `ports/repositories.py` | 只读 `DeviceRepository`、原子 `DeviceMutationUnitOfWork`、Directory Repository 与 Projector 接口。 |
| `ports/identity.py` | Clock、ID、retrieval token hash/verify、Management Authorizer 与认证后的 Management Principal。 |
| `ports/channels.py` | 唯一出站 `ChannelProviderControl` Provision/Revoke 和契约错误。 |
| `ports/management_events.py` | 带操作 `principal_id` 的低频管理审计值与只读 Stream，不是独立 Publish Port 或通用消息总线。 |

Port 表示所有权和测试边界，不表示预埋多套实现。

## 6. Adapters

| 文件 | 作用 |
|---|---|
| `adapters/persistence/models.py` | `hub_devices` 与 `hub_events` 两张表的唯一 ORM Schema。 |
| `adapters/persistence/database.py` | SQLite async engine、WAL、空库建表和非空 Schema 严格核对。 |
| `adapters/persistence/repositories.py` | SQL Device 只读 Repository、原子 Device+Audit Unit of Work 与 Management Event Ledger。 |
| `adapters/persistence/memory.py` | 纯进程内 Device Directory，不做第二份持久化。 |
| `adapters/security/enrollment_token.py` | SHA-256 Token hash 与 constant-time verify；明文不持久化。 |
| `adapters/security/management_jwt.py` | 校验管理 JWT、audience、role、Owner scope 和 `sub`，返回不可伪造的操作 Principal。 |
| `adapters/channels/provider_client.py` | HTTP Request/Reply Provider Control；封装 Bearer、超时、网络和响应契约错误。 |
| `adapters/discovery/zeroconf.py` | 在活跃 IPv4/IPv6 接口发布同链路 Descriptor/Enrollment URI，处理接口更新。 |
| `adapters/runtime.py` | 系统 Clock、安全随机 ID 与 SQLite 本地独占文件锁。 |

当前没有 Connector、MQTT、NATS、LiveKit、Data Bridge、Observability、PostgreSQL 或 migration Adapter。

## 7. Interfaces 与 Composition

| 文件 | 作用 |
|---|---|
| `interfaces/http/routers/device_onboarding.py` | 发布 Descriptor、Enrollment、Handoff，严格映射领域与 Provider 错误。 |
| `interfaces/http/routers/device_management.py` | 发布 Owner-scoped Get/List/Events、Approval/Revocation，先授权再调用 Application。 |
| `composition/resources.py` | 打开锁、SQLite、Repository、内存 Directory 与禁用环境代理的 HTTPX client；加载两个 Secret。 |
| `composition/channel_control.py` | 用 Provider URL、Token 和 HTTP client 创建 Provider Adapter。 |
| `composition/device_onboarding.py` | 组装 Token Hasher、Enroll/Handoff/Provision、Descriptor 与可选 mDNS。 |
| `composition/management.py` | 组装 Get/List、Approve/Revoke、JWT Authorizer 和事件流。 |
| `composition/app.py` | FastAPI factory/lifespan，启动重建 Directory，挂载 Router 和 `/health`。 |

Composition Root 是唯一具体实现选择位置。Router 不读取 `app.state` 或数据库。

## 8. Contracts

| 文件 | 作用 |
|---|---|
| `contracts/bindings/common.py` | 严格 Pydantic base、JSON value 和 Wire Identity。 |
| `contracts/bindings/device.py` | Manifest、Lifecycle、Directory/Page、Approval/Revocation、Event DTO。 |
| `contracts/bindings/onboarding.py` | Descriptor、Enrollment/Receipt、Handoff Request/Outcome DTO。 |
| `contracts/bindings/channel.py` | Provider Provision/Revoke 与通用 Assignment DTO。 |
| `contracts/mappers.py` | Wire DTO 与 Domain Entity 显式转换、Manifest revision 计算。 |

13 个 `contracts/schemas/**/*.schema.json` 是 Wire Contract 唯一源：

| 分组 | Schema |
|---|---|
| Common | identity、device lifecycle state |
| Device | manifest、status、directory、directory page、management mutation、management event |
| Onboarding | descriptor、enrollment、handoff |
| Channel | assignment、provider provision/revoke |

对应 13 个 `contracts/generated/schema_models/**/*_schema.py` 由 `scripts/generate_contracts.py` 生成，禁止手工编辑。`contracts/examples/device-enrollment.json` 是被 Contract Test 执行验证的唯一 Golden Example。

## 9. 数据结构与读取路径

| 数据 | 权威源 | 读路径 | 是否公开 |
|---|---|---|---|
| Identity、Manifest、Owner、lifecycle | `hub_devices` | Repository；Directory 投影 | 安全子集公开 |
| Token hash 与 retrieval window | `hub_devices` | Handoff Use Case | 否 |
| 管理审计 | `hub_events` | Owner-scoped event cursor | 是，授权后 |
| Device Directory | 内存可重建投影 | Get/List | 是，授权后 |
| Assignment/opaque binding | 当前 Handoff 调用栈 | 原样返回设备 | 仅原 Enrollment 调用方 |

Hub 内没有 Session、Challenge、online、Command、State、Event payload、Channel metadata 或 credential 持久状态。

Device Mutation 的唯一写路径是：

```text
Use Case read immutable Device snapshot
  -> DeviceMutationUnitOfWork.commit(expected, next, audit)
  -> one SQLite transaction: hub_devices + hub_events
  -> commit
  -> rebuild that Device's memory projection
```

`expected` 不匹配返回并发冲突；事件校验、唯一约束或数据库写入失败会同时回滚 Device 和 Audit。投影失败不回滚已提交权威事实，但同一 request ID 的幂等重试会重新投影。

## 10. Owner 与 Kernel

Hub 把 `owner_id` 作为稳定、不透明的 OS 根 principal ID，只保存 Device→Owner Admission。Owner profile、账号和 Persona 不进入 Hub。Kernel 读取 Hub 准入事实并拥有同一 Owner Namespace 内唯一的 Device→Companion Mount；Hub 不出现 `companion_id` 或 Kernel client。

Hub 管理入口另保留最窄的操作审计身份：Authorizer 从 JWT `sub` 生成 `ManagementPrincipal.subject_id`，Router 只把它作为 Approval/Revocation 的 `principal_id` 传入 Application，事件与幂等 fingerprint 同时绑定该值。它回答“谁执行了管理操作”，不建立第二个 Owner namespace，也不能由 request payload 指定。

## 11. 无效代码判定规则

- 表达 Handoff 后长期连接、心跳、online 或 data plane 的代码应删除。
- 在 Core 中按 MQTT/LiveKit/Provider backend 分支的代码应删除或移到 Provider。
- 重复持久化可由 `hub_devices` 重建的 Directory 应删除。
- 绕过 `DeviceMutationUnitOfWork` 单独写 Device 或 Management Event 的入口应删除。
- 未被 Composition、测试或生成流程引用，且不代表契约/Port 边界的模块应删除。
- 历史兼容和数据库 migration 不属于开发期当前产品。
