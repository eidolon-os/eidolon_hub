# ADR 0008: Hub 是契约化 Device Bus，不依赖 NATS

- Status: superseded by ADR 0013
- Date: 2026-08-01

## Context

代码核查显示，Hub 原有 NATS subjects 只有 Hub 自己的 publisher/subscriber；`eidolon_channel` 没有对应 responder 或 DataEnvelope bridge。Admin、Vision 与 Agent 的 Hub 对接路径均已有 HTTP 边界。Agent 当前读取的旧 NATS KV blackboard key/value 又与新 Device Directory 不兼容，因此保留 NATS 不能形成可工作的兼容链路。

## Decision

> “Hub 不依赖 NATS”仍然有效；“Hub 是 Device Bus”以及命令、cursor 和 data
> signaling 结论已被 ADR 0013 取代。当前 Hub 是 Device Manager。

Hub 不再连接 NATS/JetStream，也不安装 `nats-py`。它通过版本化 Device Access、Device Management 与 Provider HTTP 契约承担 Eidolon OS 的设备总线角色：

- 设备事实、租约、命令、cursor、Directory 与事件写入 Hub 自有数据库；
- WAN bootstrap 使用 Commissioned HTTPS Descriptor URI；
- Provider control/data signaling 使用认证 HTTP Request/Reply；
- 对外消费者使用 Hub API，不读取 Hub 内部数据库或 KV bucket。

## Consequences

部署少一个必须运维的集群、credential、JetStream bucket 和恢复路径；Hub 的一致性边界与 Device Bus 所有权更清楚。代价是 Hub 不提供任意 fan-out broker 语义。若未来真实代码出现多个独立消费者、长期离线回放和无法由数据库事件流满足的吞吐证据，应在 Port 外新增可选 transport Adapter，不能让 Core 重新依赖 NATS。
