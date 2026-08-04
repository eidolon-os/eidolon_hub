# Unit Test Report

- 日期：2026-08-04
- 命令：`uv run pytest -q tests/unit`
- 结果：`67 passed in 0.61s`

覆盖 Enrollment 内容幂等/冲突、Token hash/verify、窗口过期、三态 approval/revoke、Handoff/Provision、Provider policy 解耦、opaque relay、Management JWT、Zeroconf、memory Directory、Owner-scoped Get/List/filter/cursor、严格 Local-only 配置、SQLite 文件锁与当前两表 ORM Schema check。

Domain + Application branch suite 为 `81 passed`，432 statements、114 branches，总覆盖率 `97%`，超过 90% 验收线。
