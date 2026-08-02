# Deployment Mode Report

- 命令：`uv run pytest -q tests/functional/test_deployment_mode_switch.py`
- 结果：2 passed in 0.90s

Local（SQLite + mDNS）与 Cloud（PostgreSQL + no mDNS）由同一 artifact 发布完全相同的 HTTP paths 和 OpenAPI schemas。`EIDOLON_HUB_PROFILE` 只选择行为、Discovery 和 Persistence Adapter；不包含数据迁移、自动桥接或 Session replication 语义。
