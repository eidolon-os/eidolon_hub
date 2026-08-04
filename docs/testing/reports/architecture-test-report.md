# Architecture Test Report

- 日期：2026-08-04
- 命令：`uv run pytest -q tests/architecture`
- 结果：`25 passed in 0.27s`

门禁验证分层 import 方向；Core 无基础设施依赖；Hub 无 MQTT、NATS、SDK/Data、LiveKit、`eidolon_channel`、PostgreSQL 或 telemetry；无 Session/Challenge/Heartbeat/online、Command/DataEnvelope/Data Bridge；无重复 Directory 表、Channel persistence/lifecycle 或 Provider ingress；SQLAlchemy 受限于 Persistence/Composition；Router 不使用 Service Locator；配置只有单一 `settings.yaml`；数据库无 migration 或旧结构兼容；clean build 不包含已删除源码。
