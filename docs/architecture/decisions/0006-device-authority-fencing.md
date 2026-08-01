# ADR 0006: Device Authority 使用 CAS 与 fencing token

- Status: accepted
- Date: 2026-08-01

## Context

多个 Hub instance、Connector 切换和网络分区可能同时声称拥有同一设备。如果只依赖 heartbeat timestamp，旧 instance 恢复后会覆盖新 owner 的状态。

## Decision

每个设备有 durable `DeviceAuthorityLease`。Acquire/renew/validate 经 Hub PostgreSQL Adapter 的事务、行锁与 version 执行；Local SQLite 为单实例。所有 Connection 都携带 authority 的 `hub_instance_id` 和严格递增 `fencing_token`。低 token 写入和续约必须失败。

## Consequences

并行 Connector 不等于并行 authority owner。Failover 可通过明确 lease expiry/acquire 完成；Local 与 Cloud 部署的 authority store 仍隔离，不自动争抢或桥接。
