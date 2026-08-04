# Local Functional Report

- 日期：2026-08-04
- 命令：`uv run pytest -q tests/functional`
- 结果：`4 passed in 0.34s`

覆盖 HTTPS Descriptor/Enrollment、pending Handoff、人工审批、approved Handoff、真实 HTTP Reference Provider Provision、opaque binding 同响应转交、显式 Revoke，以及 Provider 响应/网络错误边界。测试不包含长期设备连接或业务 data round trip。
