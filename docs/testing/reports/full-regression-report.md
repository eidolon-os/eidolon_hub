# Full Regression Report

- 日期：2026-08-02
- 总测试：133
- 默认命令：`uv run pytest -q` → 131 passed, 2 skipped（无 PostgreSQL DSN）
- Cloud 命令：`EIDOLON_HUB_TEST_POSTGRES_DSN=postgresql://127.0.0.1:5432/eidolon_hub_test uv run pytest -q` → 133 passed in 5.41s

回归覆盖 Architecture、Unit、Contract、Component、Local/Cloud Functional、Deployment Mode 和 Contract E2E。外部兄弟项目和生产故障注入不在本结果中。
