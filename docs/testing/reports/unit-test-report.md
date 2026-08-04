# Unit Test Report

- 日期：2026-08-05
- 命令：`uv run pytest -q tests/unit`
- 结果：`71 passed in 2.31s`

覆盖 Enrollment 内容幂等/冲突、canonical fingerprint、Token、三态 policy、Handoff/Provision、opaque relay、Management JWT、独立精确只读服务 Token 权限、Zeroconf、memory Directory、Owner-scoped query、严格配置、SQLite 锁与两表 Schema。

命令 `uv run pytest -q tests/unit tests/component tests/functional --cov=hub.domain --cov=hub.application --cov-branch --cov-fail-under=90` 的结果为 `87 passed in 3.45s`，441 statements、114 branches，总覆盖率 `97.30%`，超过 90% 验收线。
