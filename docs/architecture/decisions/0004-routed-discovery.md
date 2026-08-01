# ADR 0004: 路由子网使用 URI 或 Unicast DNS-SD，不桥接 mDNS

- Status: accepted
- Date: 2026-08-01

## Context

mDNS multicast 的有效范围是单链路。跨 VLAN 反射 multicast 会放大流量、破坏故障域，并不能提供稳定的身份、认证和 Commissioning 语义。RFC 8766 Discovery Proxy 使用单播 DNS 侧的发现机制，并不会使任意标准 mDNS browse 调用透明获得跨网段结果。

## Decision

所有设备必须能在 Commissioning 时保存 HTTPS Descriptor URI。同链路可用 mDNS 优化体验；路由网络可通过 `dnspython` Unicast DNS-SD 发布和查询。SRP/Discovery Proxy 由网络基础设施提供，Hub 不实现，也不跨 VLAN 泛洪 mDNS。

这套要求针对契约能力而不是 ESP32 或任何特定硬件。纯 mDNS 设备必须增加 URI/Unicast DNS 路径，不能假定“网络侧无感知”。

## Consequences

显式 URI 是所有网络形态的可靠 fallback。网络管理员可选择标准 DNS-SD/SRP/Proxy，但设备 SDK 的能力必须在 Conformance Test 中明确声明。
