# Failure and Security Report

已验证：challenge 单次消费/P-256 nonce binding、Session token/device/fingerprint/expiry、heartbeat sequence、authority fencing、JWT role/owner/audience、Provider Bearer callback、Provider Acquire outage 不修改注册或已有 Channel metadata、Provider response identity/kind/state/lifetime 校验、Session+Channel 双门禁、Envelope device/command/cursor 校验、DB-first cache、opaque binding 的 repr/SQL/Directory 隔离、未知/旧配置字段拒绝、Cloud instance ID/OTEL endpoint 缺失拒绝，以及未迁移数据库 revision 拒绝启动。

未验证：生产证书签发/撤销、TLS/mTLS、Provider/DB restart、credential 泄露扫描、网络分区、限流/DoS、PostgreSQL failover、rolling upgrade。以上仍是部署门禁，不能标记 Passed。
