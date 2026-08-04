# Contract Test Report

- 日期：2026-08-04
- 命令：`uv run pytest -q tests/contract`
- 结果：`15 passed in 3.90s`

验证 13 个 Draft 2020-12 Schema、生成模型 freshness、Enrollment golden example、三态 lifecycle、typed Directory/Page/Event、Descriptor/Enrollment/Handoff，以及 Provider Provision/Revoke 固定 path/request/response 和 opaque base64 relay。

契约中不存在 Session、Heartbeat、online、MQTT/Connector、Command、DataEnvelope、Channel Lifecycle 或 Provider ingress。
