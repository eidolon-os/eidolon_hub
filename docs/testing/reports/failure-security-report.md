# Failure and Security Test Report

- 日期：2026-08-01
- 自动化层状态：Passed（包含在 Unit/Contract/Component/Functional suites）
- 部署故障状态：Blocked / Not Run

已验证：challenge 单次消费与重放拒绝、P-256 nonce/fingerprint binding、JWT audience/role/owner/expiry、MQTT Topic identity 与 operation allow-list、Provider bearer callback、Grant 投递失败恢复旧 active lease、Provider failure/backoff、SQL claim takeover、DataEnvelope duplicate/乱序/device-channel/TTL 校验、DB-first cache write failure，以及 opaque binding 的 repr/log/SQL/Directory 隔离。

远端 Provider 必须 HTTPS；loopback 才允许 HTTP。Provider egress 不继承 ambient proxy，避免凭据被环境代理转发。

尚未验证：真实 Broker/Provider/PostgreSQL restart、网络分区、rolling restart、TLS/mTLS 与 Broker ACL、证书撤销、OTLP collector 故障。另一个发布前一致性工作是把 Device/Connection fact 与 durable event append 放入同一数据库 Unit of Work；目前两者是顺序事务，进程在中间崩溃可能漏掉不可重建的历史事件。全量通过不覆盖这个故障窗口。
