# Deployment Mode Test Report

- 日期：2026-08-01
- 状态：Passed
- 命令：`uv run pytest -q tests/functional/test_deployment_mode_switch.py`
- 结果：`2 passed in 0.68s`

同一 Hub artifact 在 Local 配置（SQLite + mDNS）和 Cloud 配置（PostgreSQL + MQTT5）下发布完全相同的 HTTP paths 和 OpenAPI component schemas；差异只由 Composition Root 选择 Adapter/Connector。

测试还锁定不存在 `migration_source`、`bridge_to_cloud` 或 live lease replication 配置。这里的“无缝切换”只表示代码/契约无分叉，不表示 SQLite 数据自动迁移、Local/Cloud 自动桥接或活跃 Connection/Channel lease 跨部署转移。
