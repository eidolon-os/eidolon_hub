# Contract Test Report

- 日期：2026-08-01
- 状态：Passed
- 命令：`uv run pytest -q tests/contract`
- 结果：`25 passed in 2.83s`
- 生成一致性：`uv run python scripts/generate_contracts.py --check`，Passed

验证全部 Draft 2020-12 JSON Schema、golden examples、Pydantic Wire DTO、MQTT operation/topic allow-list、Connector/Provider conformance、Provider sync、lifecycle callback 和双向 DataEnvelope HTTP binding。

Channel 契约只公开 typed device facts（包含 tenant/owner 安全归属）、通用 `purpose/kinds/binding_format/lease` 和 opaque base64 binding；没有 Profile、Provisioner、LiveKit、Room、TURN 或 Codec 字段。MQTT inbound 不包含 Command、State、Event、Data 或设备 Channel negotiation。

限制：Schema 合规不证明生产 Broker ACL、TLS/mTLS、证书撤销或外部 Provider 已实现该契约。
