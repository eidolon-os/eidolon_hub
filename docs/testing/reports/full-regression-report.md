# Full Regression Report

- 日期：2026-08-05
- 命令：`uv run pytest -q`
- 结果：`130 passed in 12.70s`，无 skip

回归覆盖 Architecture、Unit、Contract、Component、Local Functional、Contract E2E 和 Workspace Integration，包括独立 Kernel reader token 的最小权限，以及真实 Hub Approval/Get/Revocation→Kernel CAS tombstone 链路。

Kernel consumer 既由 Kernel 仓库独立验证，也由 Hub 工作区联合测试验证；真实小程序、真实设备、`eidolon_channel`、生产配网、TLS/DNS/VLAN 和生产网络故障仍不在本结果中。
