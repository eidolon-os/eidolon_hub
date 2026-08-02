# Architecture Test Report

- 命令：`uv run pytest -q tests/architecture`
- 结果：15 passed in 0.26s

架构门禁验证 Domain/Application 不导入基础设施；Hub 无 transport Connector、MQTT、NATS、`eidolon_data`、`eidolon_sdk`、LiveKit 或 `eidolon_channel` 依赖；SQLAlchemy 被限制在 Persistence/Composition；Router 无 Service Locator；Persistence 不含 opaque binding。

临时 clean wheel 构建成功；发布文件名中 `mqtt|connection|reconcile|provider_sync|asyncapi|signaling|grant_sender` 匹配为 0，确认删除项没有从历史 build artifact 回流。
