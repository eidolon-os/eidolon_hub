# Full Regression Report

- 日期：2026-08-04
- 命令：`uv run pytest -q`
- 结果：`128 passed in 11.32s`，无 skip

回归覆盖 Architecture、Unit、Contract、Component、Local Functional 和 Contract E2E，包括 Enrollment Token/窗口、三态策略、JWT `sub` 操作审计、canonical fingerprint delimiter-collision、Device+Audit 原子提交/回滚、并发 expected snapshot、幂等投影修复、两表 ORM、内存 Directory 重建、Get/List/Event、请求级 Provider Handoff 与 opaque 隐私边界。

Kernel Hub consumer 由 Kernel 仓库独立验证；真实 Companion Authority、真实兄弟项目、生产配网链路、TLS/DNS/VLAN 和生产故障注入不在本结果中。
