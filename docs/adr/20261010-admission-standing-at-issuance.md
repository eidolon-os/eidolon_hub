# 准入：Owner 的决定在签发 voucher 时记录，Proposal 到达即由它裁决

状态：已实现（Hub + Admin），待真机验收。

## 问题

一台设备被 Owner 接纳只发生在一个时刻：Owner 已接受的 Controller（手机）在设备旁、
在 SoftAP 会话里，为设备刚出示的密钥向本 Host 要一张 voucher。此后设备把这张 voucher
带给准入 Authority，Authority 却只把它当作一份匿名的 proof：Proposal 进入
`pending_review`，再让 **同一个 Owner** 从队列里对 **同一件事** 第二次表态，并且给这次
表态 15 分钟（`proposal_ttl`）。

2026-10-10 的真机记录（`eidolon_client_mobile/docs/device-setup-host-return.md`）：
korvo-1 00:56:51 提交 Proposal 并每几秒 `collect` 一次（160 余次 409 `DECISION_REQUIRED`）；
手机因为换网后找不到换了 IP 的 Host，15 分钟内没能批准；01:11:51 过期，固件收到 410 后进入
"Waiting for Owner to restore device access" 的终态——而 Hub 并没有这条恢复路径，App 对过期的
Proposal 也批不了。三方各自合理，合起来是一条断路。

## 决定

- **签发前先记录 standing。** Admin 铸好 voucher 的 `jti` 后，先向 Hub
  `POST /api/admission/v1/commissioning-standings {jti, operational_key_id}`（携带与
  Decision 相同的 Controller ActorRef，要求 `device.claim.approve`），Hub 落
  `admission_commissioning_standings_v1`，并回答与 `GET /base-identities` 相同的身份答案；
  Admin 再用这个 `jti` 签 voucher。Hub 从此真的「保存签发记录」，而不只是消费记录。
- **Proposal 到达即裁决。** `create_enrollment` 在同一事务里：voucher 方案按 `jti` 找 standing，
  续用方案（`enrolled-base-key-v1`）按密钥找最近一条 standing；找到且属于本 Owner Domain、
  为同一密钥、记录者持有 `device.claim.approve`，就用 **记录者** 作为 actor 写下 approve
  Decision、铸 Grant、发 `enrollment-approved` 事件——与 Controller 从队列裁决走完全同一段
  代码（`_decide_in_session`）。审计里「谁批准的」= 当初签 voucher 的那台手机。
- **对设备说的仍是 Proposal 创建时的样子。** `CreateEnrollmentResult.state` 契约为
  `pending_review`、revision 1，固件据此 `collect`；Decision 在 revision 2，与人工裁决一致。
  设备的下一次 `collect` 直接拿到 Grant。
- **没有 standing 的 Proposal 行为不变**：留在队列等 Controller 裁决。被 Owner 拒绝/移除过的身份
  仍由 `requires_fresh_presence` 挡在 Proposal 之前；新的在场配网产生新的 standing。
- `decide_from_standing` 构造参数可关闭自动裁决（测试与排障用），默认开启。

## 撤销 Controller 即撤回它的 standing（2026-10-11 补）

standing 的价值在于它比记录它的那一刻活得久——voucher 未被使用前一直有效（24 小时）。反面是：
Owner 撤销记录它的那台手机（例如手机丢失）之后，这台手机此前签出、尚未被设备用掉的 voucher
仍会以它的名义自动批准。一次性 jti 账本只防重放，不防「签发后撤销」。

- **Host 在撤销授权之前先告诉 Hub。** Local API 撤销 Controller 时，先经控制面
  `POST /api/admission/v1/controller-withdrawals {principal_id}`（携带发起撤销的 Controller 的
  ActorRef，要求 `device.claim.approve`；手机可以撤销自己），Hub 落
  `admission_controller_withdrawals_v1(owner_domain_id, principal_id, withdrawn_at, actor_json)`；
  成功后才让 bootstrap 撤销 grant。Hub 不可达则撤销被拒绝——反过来的顺序会留下一个窗口。
  Host 没有 Workspace 或没有 Owner Domain 准入配置时从未签过 voucher，直接撤销、不问 Hub。
- **裁决时忽略被撤回者的 standing。** `_standing_for` 若发现记录者在 `recorded_at` 当时或之后被撤回，
  返回无 standing：Proposal 进队列，等 Owner 仍在使用的 Controller 裁决。续用申请同样适用。
- 撤回按 Controller 记一行而不是给每条 standing 打标：它覆盖该 Controller 记录过的全部 standing，
  包括 voucher 还没出示的；同一把手机密钥以后被重新授权，之后记录的 standing 照常有效；
  再次撤回只会把时刻往后推，不会往前挪。

**未覆盖**：运维 `controller-reset`（全部手机丢失后的物理在场恢复）走 bootstrapd 控制 socket，
不经 Local API，因此不会通知 Hub；旧手机签出的未用 voucher 在 24 小时内仍可能被自动批准。

## 没有做的

- 没有改 voucher 的 claim 集。把 actor 放进 voucher 也能达到同样效果，但那是签名契约
  （SDK golden、Hub/Admin/固件四处 vector）的变更；Hub 记录自己的台账是 Hub 本来的职责。
- 没有去掉 Decision 阶段或审核队列：无 standing 的申请（设计文档 D9 禁止「任意设备随时重申请」）
  仍需人工。
- 没有改 `proposal_ttl`。过期后的自愈属于固件侧（`PROPOSAL_EXPIRED` → `AbandonPendingProposal`
  → 重新 propose，续用方案会再次自动获批），另行落地。

## 验证

`tests/unit/admission/test_commissioning_standing.py`：standing 即裁决且 collect/ack 走通；
无 standing 仍排队；过期后续用申请由同一 standing 裁决；缺 approve scope 不能记录、密钥不符不裁决；
记录幂等且同 jti 异义拒绝（`IDEMPOTENCY_CONFLICT`）；开关关闭回到人工；移除后只有新的在场
voucher 才再次裁决、且 claim generation +1；HTTP 路由与严格载荷。Admin
`test_commissioning_voucher_issuance.py` 覆盖「Hub 记录的 jti 就是设备携带的 jti」。
