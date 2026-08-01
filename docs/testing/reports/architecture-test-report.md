# Architecture and Dependency Test Report

- 日期：2026-08-01
- 状态：Passed
- Architecture：`uv run pytest -q tests/architecture` → `15 passed in 0.35s`
- Import Linter：`108 files, 179 dependencies, 3 contracts kept`
- Ruff：`ruff check` Passed；`ruff format --check` → `148 files already formatted`
- Production dependency：`uv tree --frozen --depth 1 --no-dev` Passed
- Clean wheel：`uv build --wheel` Passed；最终 `135` entries，最新 owner contract 与 remote HTTPS guard 已打包，旧实现文件与 NATS/Data/SDK/LiveKit dependency matches 均为 `0`

AST/metadata 测试禁止 Domain/Application 导入 FastAPI、SQLAlchemy、MQTT、Zeroconf、LiveKit、NATS、`eidolon_data` 或 `eidolon_sdk`；禁止 Router 访问 DataStore；禁止 Connection Plane 导入 Channel 实现；禁止 Profile/Provisioner 旧模型和 Persistence 保存 `opaque_binding`。

Composition Root 现按 resources、connection plane、channel control 和 device management 拆分；Clock 与 ID 的生产实现归属 Runtime Adapter，Composition 只选择并装配实现。

一级生产依赖只有当前 Adapter 所需库；树中没有 NATS、`eidolon_data`、`eidolon_sdk` 或 LiveKit。

第一次 wheel 检查曾因历史 `build/lib` 未清理而重新打入已删除文件。精确生成目录已移到 `/private/tmp` 备份，从空 build/egg-info 重建后通过上述清单检查。它证明发布验收必须使用 clean build，而不能只检查 Git source。
