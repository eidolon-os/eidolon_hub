# ADR 0005: Provider 通过标准 DataEnvelope Bridge 接入 Core

- Status: accepted
- Date: 2026-08-01

## Decision

WSS、LiveKit 与未来 Provider 在设备侧终止实际 Channel，并把普通管理数据转换成统一 `DataEnvelope`。Hub 只提供有方向、可认证的 Provider Bridge；当前 Binding 是 HTTP，由 Hub/Provider OpenAPI 描述。MQTT AsyncAPI 只描述 Connection lifecycle 与 Channel signaling。

Command 只从 Core 发向设备。设备只可返回 Ack、Result、ReportedState 和 DeviceEvent。每个 Channel 使用 durable monotonic cursor 去重，并在入口再次验证 Channel Lease、device ID 和 TTL。Audio/Video 不转换为 Envelope。

## Consequences

Core 不导入 HTTP、WebSocket 或 LiveKit 类型。Provider 可独立扩缩容和选择区域，Hub 重启后数据库 cursor 仍存在。MQTT Connection Connector 没有业务数据发送 API，因此不会退化为另一个 Data Plane。Hub 不需要通用消息总线来完成 Provider 对接。
