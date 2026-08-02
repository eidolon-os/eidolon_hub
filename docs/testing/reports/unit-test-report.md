# Unit Test Report

- 命令：`uv run pytest -q tests/unit`
- 结果：97 passed in 0.64s

覆盖 Session/Authority 值与 heartbeat、注册/审批/吊销、Channel Acquire 校验与 Provider outage、Lifecycle、Command 状态机、Session+Channel 双门禁、Envelope/cursor、P-256/JWT、Zeroconf、memory Directory、真实 OTEL 配置语义、严格 Local/Cloud Profile、credential-free PostgreSQL target/环境凭据合成、pool 参数和 Alembic revision/head check。

Domain + Application branch 门禁命令覆盖 Unit/Component/Functional 共 117 项，结果为 98%（795 statements、234 branches），超过 90% 验收线。
