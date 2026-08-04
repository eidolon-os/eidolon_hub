# Hub / Kernel Integration Report

- 日期：2026-08-05
- Hub 命令：`uv run pytest -q tests/integration`
- Hub 结果：`1 passed in 2.48s`
- Kernel 全量：`82 passed in 3.18s`
- Kernel branch coverage：`94.74%`
- Data 全量：`55 passed in 37.68s`

联合测试启动真实 Hub Composition 与独占临时 SQLite，完成 Enrollment、管理 Approval、Kernel reader 精确 Get、真实 Provider Revoke、Hub Revocation，再用 Kernel 当前 consumed Schema、mapper、SQLite Mount Store 与 Reconciliation use case 将 active Mount 原子变为 revision 2 tombstone。

Data 仓库另有真实 Companion Authority→Kernel consumer 联合测试，证明独立 Authority App 的严格 Identity response 可被 Kernel 当前 adapter 解析，且 profile/runtime metadata 不会泄露。

已证明的是本地工作区中的跨仓库控制面契约。尚未证明真实小程序编排、真实固件、`eidolon_channel`、进程级服务启动顺序、生产 TLS 或复杂网络行为。
