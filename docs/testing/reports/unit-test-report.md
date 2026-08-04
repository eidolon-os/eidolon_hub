# Unit Test Report

- 日期：2026-08-04
- 命令：`uv run pytest -q tests/unit`
- 结果：`70 passed in 1.37s`

覆盖 Enrollment 内容幂等/冲突、canonical fingerprint 分隔符碰撞、Token hash/verify、窗口过期、三态 approval/revoke、幂等重试修复提交后投影失败、Handoff/Provision、Provider policy 解耦、opaque relay、Management JWT 及已验证 `sub` Principal、Zeroconf、memory Directory、Owner-scoped Get/List/filter/cursor、严格 Local-only 配置、SQLite 文件锁与当前两表 ORM Schema check。

命令 `uv run pytest -q tests/unit tests/component tests/functional --cov=hub.domain --cov=hub.application --cov-branch --cov-fail-under=90` 的结果为 `86 passed in 3.69s`，441 statements、114 branches，总覆盖率 `97.30%`，超过 90% 验收线。
