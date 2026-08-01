# Local Functional Test Report

- 日期：2026-08-01
- 状态：Passed
- 命令：`uv run pytest -q tests/functional/test_external_channel_provider_flow.py tests/functional/test_https_connection_flow.py tests/functional/test_mqtt_connection_flow.py`
- 结果：`6 passed in 0.34s`

覆盖 HTTPS Hello→Challenge→Proof→Register→Heartbeat、重放拒绝；MQTT5 binding 的窄 operation、Topic identity 和 Grant outbound；以及 SQLite 上设备事实→审批→Provider HTTP sync→opaque Grant→lifecycle active→Command→Ack/Result 的闭环。

Provider 功能流使用 ASGI HTTP server，证明序列化、认证、固定路径和用例组合，不冒充真实 TCP/进程故障。真实 TCP Provider 证据见 Contract E2E 报告；mDNS 和复杂子网实网仍属于部署验收。
