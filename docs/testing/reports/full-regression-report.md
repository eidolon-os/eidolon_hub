# Full Regression Report

- 日期：2026-08-02
- 总测试：146
- 默认命令：`uv run pytest -q` → 144 passed, 2 skipped in 5.19s（无 PostgreSQL 测试 DSN）
- Cloud 命令：`EIDOLON_HUB_TEST_POSTGRES_DSN=postgresql://127.0.0.1:5432/eidolon_hub_test uv run pytest -q` → 146 passed in 5.25s

回归覆盖 Architecture、Unit、Contract、Component、Local/Cloud Functional、Deployment Mode 和 Contract E2E。新增证据包括严格 Profile 选择、无 dotenv Cloud 配置、OTEL endpoint fail-closed、Alembic revision/idempotency/head check、PostgreSQL 隔离空 Schema 迁移，以及 credential-free YAML DSN 与特殊字符环境凭据的结构化合成。外部兄弟项目和生产故障注入不在本结果中。
