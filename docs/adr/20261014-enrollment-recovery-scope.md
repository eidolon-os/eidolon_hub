# Enrollment recovery 与设备当前授权分离

状态：已实现；2026-10-14。

## 问题

手机在旧 Claim 被撤销后，以同一身份提出新登记。新 Grant 已签发，
`_recovery_projection` 却只按 device_instance_id 取 Claim，把上一代 revoked
Claim 附给新登记。消费者优先采纳 Claim，因而无法推进 collect / ACK。

## 决定

EnrollmentRecoveryProjection 描述指定登记操作；Claim 查询和 Device Control
描述设备当前授权。两者不能通过设备身份相同就互相替代。

恢复结果中的 Claim 必须与本次 Grant 的 approval Decision、claim generation
及 Owner Domain 一致。新 Grant 尚未 ACK 时，旧 Claim 留在授权账本中继续生效
（包括 revoked），但不进入新登记的恢复结果。新 Claim 激活后也不能反向出现在
较早登记的恢复结果中。trust_epoch 可以在同一 Claim 内演进，不以初始 epoch
的相等性排除该 Claim 的当前撤销事实。

沿用既有契约及字段，不新增授权入口、不迁移或改写历史 Claim、不放宽签名或
撤销校验。Mobile 对旧 Host 的兼容遵循登记阶段：未 ACK 的登记不能被附带的
旧 Claim 终结。真正进入对话仍必须通过 Device Control 的当前授权验证。

## 验证

`test_a_removed_body_is_decided_again_only_by_a_fresh_standing` 覆盖真实 Authority
事务：第一代激活、撤销、拒绝无在场证明重入、新 standing 批准、恢复、领取、
ACK、第二代激活，以及新旧登记不能互相冒充。相关 Admission / Device Control
及 stale-ref 回归 100 项通过。
