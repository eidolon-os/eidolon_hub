# Cloud Functional Report

- 环境：本机 PostgreSQL 18，`127.0.0.1:5432/eidolon_hub_test`
- 命令：`EIDOLON_HUB_TEST_POSTGRES_DSN=postgresql://127.0.0.1:5432/eidolon_hub_test uv run pytest -q tests/functional/test_cloud_infrastructure.py`
- 结果：2 passed in 0.27s

验证真实 asyncpg/SQLAlchemy round-trip，以及两个独立 Hub database clients 并发获取同一 device authority 时恰有一个成功、另一个被 fencing 拒绝。没有验证生产 TLS、DB restart、网络分区或 rolling upgrade。
