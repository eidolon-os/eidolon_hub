# Failure and Security Report

已验证：高熵 Token Wire 长度、Token 只保存 SHA-256 hash、constant-time verify、422 校验响应移除原始 `input`、错误 Token 403、过期窗口 410、未审批不 Provision、revoked 终态、JWT role/owner/audience/subject、Approval/Revocation 审计绑定已验证 JWT `sub` 且不信任 payload、canonical fingerprint 分隔符无碰撞、Provider 契约错误 502/不可达 503、内容幂等冲突 409、Device+Audit 原子回滚、expected snapshot 并发冲突、event ID 复用回滚、幂等重试修复投影、opaque binding 的 repr/SQL/Directory 隔离、查询输入和集合大小有界、严格 Local-only 配置、同 SQLite 第二进程 fail closed。

产品必须额外保证：小程序通过二维码/短码/BLE/SoftAP/物理确认把用户操作的真实设备与 Enrollment 绑定。retrieval token 不是设备制造身份认证；仅按设备自声明 ID 批准是不安全的。

当前实现因尚无该 pairing contract，只允许受信 `hub-admin` 审批 pending 设备；普通 `device-manager` 不能认领未绑定 Owner 的设备。

未验证：生产 TLS/mTLS、密码学 Hub trust anchor、配网限流/DoS、Token 泄漏后的响应、真实 Companion Authority、Provider/DB restart、网络分区、真实 Channel credential 撤销和残余有效窗口。PostgreSQL failover、rolling upgrade、多实例、Session 和 online 不是当前 Hub 产品能力。
