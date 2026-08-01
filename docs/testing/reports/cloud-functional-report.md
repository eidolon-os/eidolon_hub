# Cloud Functional Test Report

- 日期：2026-08-01
- 状态：Passed for available local infrastructure
- PostgreSQL：`18.4 (Postgres.app)`，`127.0.0.1:5432`
- 隔离数据库：`eidolon_hub_test`（由本轮创建并保留，测试行已清理）
- MQTT：Homebrew Mosquitto，`127.0.0.1:1883`
- 命令：`EIDOLON_HUB_TEST_POSTGRES_DSN=postgresql://127.0.0.1:5432/eidolon_hub_test EIDOLON_HUB_TEST_MQTT_HOST=127.0.0.1 EIDOLON_HUB_TEST_MQTT_PORT=1883 python -m pytest -q tests/functional/test_cloud_infrastructure.py`
- 结果：`3 passed in 0.80s`（最终复验）

真实验证内容：

- PostgreSQL schema 初始化与 Hub Device Repository round-trip；
- 两个独立 PostgreSQL engine/pool 并发竞争相同 Channel sync intention，唯一约束/行锁/retry 后恰好一个 claim owner；
- Mosquitto 上 MQTT5 QoS1 subscribe/publish/request-reply 与 Connector start/stop。

限制：Mosquitto 测试使用本机明文 1883，未证明生产 TLS/mTLS、ACL 或证书撤销；没有注入 Broker/PostgreSQL restart、网络分区或 Hub rolling restart。生产 Composition 的 MQTT Connector 强制 TLS，因此本报告不是生产 Cloud deployment E2E。
