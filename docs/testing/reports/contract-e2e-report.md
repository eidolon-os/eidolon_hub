# Contract E2E Test Report

- 日期：2026-08-01
- 状态：Passed
- 命令：`uv run pytest -q tests/e2e`
- 结果：`1 passed in 6.20s`

黑盒链路使用生产 Composition、临时 SQLite、可选内存 Directory、真实 TCP Reference Provider，以及只依赖发布 Wire Contract 的 Reference Device。它完成真实 P-256 challenge proof、注册、管理审批、Grant mailbox poll、Provider lifecycle active、Command outbound、Ack/Result inbound、Directory 查询和 Hub 重启后 Directory 恢复。

首次运行发现 `httpx` 继承环境代理，把 localhost Provider 和 bearer credential 重定向到 `127.0.0.1:7890` 并返回 502。生产 Provider egress 已改为 `trust_env=False`，使契约地址成为唯一路由权威并避免 ambient proxy 泄漏凭据。

限制：该 E2E 不启用真实 mDNS、MQTT Broker、PostgreSQL、LiveKit、WSS 或音视频；它证明 Hub/Provider 边界，而不是兄弟项目已经完成迁移。
