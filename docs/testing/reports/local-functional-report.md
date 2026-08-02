# Local Functional Report

- 命令：`uv run pytest -q tests/functional/test_https_device_access_flow.py tests/functional/test_external_channel_provider_flow.py`
- 结果：3 passed in 0.22s

链路覆盖 HTTPS Session Hello/Proof/Register/Heartbeat、challenge replay 与错误 credential 拒绝、主动 Channel Acquire、opaque binding 直返、Provider lifecycle active、Command outbound 和 Ack/Result inbound。
