# ADR 0014: 暂缓 Hub 内置第三方 Observability 集成

- 状态：Accepted
- 日期：2026-08-03
- 取代：ADR 0012 中的内置 OpenTelemetry 决策

## 背景

Hub 曾内置完整的 OpenTelemetry OTLP/gRPC runtime，包括 HTTP middleware、Trace/Metric/Log provider 和 exporter。当前项目尚未确定 telemetry 平台、采样、字段脱敏、成本和生产运行要求，提前链接 SDK 会扩大依赖、启动配置和故障面，却没有已验证的消费端。

## 决策

1. 删除 Hub 的 Observability Adapter、middleware、runtime lifecycle、配置、环境变量和 telemetry 依赖。
2. Hub 保留 Python/Uvicorn 标准日志，通过进程标准输出提供最小运行诊断。
3. 不创建空的 Observability Port 或 no-op abstraction；当前没有可替换实现的业务需求。
4. 部署环境可以在 Hub 进程外采集标准输出，但这不是 Hub Contract。
5. 未来若引入 telemetry，必须先定义消费平台、信号、采样、敏感数据、失败语义和验收测试，再以新 ADR 和独立 Adapter 实施。

## 结果

Local 配置不含 Observability 字段，也不需要 OTLP endpoint。Hub 的设备管理契约和业务行为不受影响。代价是当前没有应用内 trace/metric exporter；这是明确接受的边界，不宣称已具备平台可观测性。
