# Full Regression Report

- 日期：2026-08-01
- 状态：Passed with local PostgreSQL/MQTT infrastructure
- 命令：`uv run pytest -q -rs`
- 结果：`158 passed in 10.77s`
- 全 Hub branch coverage：`78%`（coverage run：`158 passed in 13.63s`；真实 MQTT Adapter coverage `83%`）
- Domain/Application branch coverage：`95%`

运行时注入本机隔离 PostgreSQL 18 DSN 和 Mosquitto 地址，无 skip、失败或 xfail。结果包含 Composition Root 拆分后的 Runtime Adapter 单元测试。

全 Hub 覆盖率包含自动生成 DTO、保留的 Guard/Sense 公共契约、Composition、网络 Adapter 和 Router；因此低于 Domain/Application。低覆盖但由 Composition 或发布契约引用的代码没有仅凭数字删除，删除依据是生产 import、所有权、契约兼容和测试语义共同判断。
