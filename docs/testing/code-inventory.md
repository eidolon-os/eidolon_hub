# Hub 逐文件有效性审计记录

- 审计日期：2026-08-04
- 当前代码说明：[代码架构与完整代码导览](../code-architecture.md)

## 审计方法

逐一核对生产 Composition、Application/Port 调用方、Router、Repository、Schema、生成流程和测试，并以 [Device Onboarding Subsystem 标尺](../architecture/device-control-subsystem.md) 判断所有权。“仅被旧测试引用”不是保留生产代码的理由；目标边界删除后，对应测试、Schema、配置和依赖一起删除。

## 当前保留责任

| 层 | 必要责任 |
|---|---|
| Domain | Device identity/Manifest、三态 policy、Provider-neutral 请求级 Assignment |
| Application | Enroll/Handoff/Provision、Approve/Revoke、Get/List、Directory projection |
| Ports | Device/Directory Repository、Token、授权、Provider Control、管理审计 |
| Contracts | 13 个 Schema、严格 Binding、generated shape、Mapper、1 个 golden example |
| Adapters | 两表 SQLite、内存 Directory、Provider HTTP、Zeroconf、Token hash/JWT、exact-Get opaque capability、Clock/ID/锁 |
| Interfaces | Device Onboarding 与 Device Management API |
| Composition | Secret、资源、业务图与 ASGI 生命周期的唯一组装点 |

逐文件作用见 [当前代码导览](../code-architecture.md)。

## 本轮新增删除结论

设备批准后由 Channel Provider 完全接管，所以下列概念不再有 Hub 内消费者，已连同代码、契约、配置和测试删除：

- Device Challenge/Proof 和 P-256 初始认证；
- 持久 Device Session、Heartbeat、Lease、Close；
- Hub `online`、Session count/expiry 与周期 Directory Worker；
- 重复的 `hub_device_directory` 持久投影表；
- Device Registration/Registration Outcome，改为 Enrollment/Handoff；
- Lease HMAC Secret 及直接 `cryptography` 依赖。

此前已删除的 Connector/MQTT、NATS、Command/DataEnvelope/Data Bridge、Channel persistence/lifecycle、LiveKit、SDK/DataStore、PostgreSQL/Cloud、migration、Observability 不恢复。

## 当前门禁

Architecture、Import Linter、Contract freshness、Ruff、两表 Component、Functional/E2E、Kernel consumer Integration 和 clean build 共同阻止已删逻辑回流。最近结果见 [当前测试基线](current-baseline.md)。
