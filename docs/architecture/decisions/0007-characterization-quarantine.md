# ADR 0007: Characterization Harness 的临时隔离与删除

- Status: superseded by completed removal
- Date: 2026-08-01

## Decision

重构前先将旧 FastAPI/LiveKit/blackboard/guard runtime 放入临时 `hub/legacy`，只用于固定有价值的语义。对应 Domain、Contract、Port 与 Use Case 测试落地后，临时目录、旧 root tests 和 LiveKit dev dependency 整体删除。

## Consequences

Hub 源码和 artifact 不再包含旧 register/token、LiveKit online/send 或 snapshot blackboard。Guard/Sense wire 语义由 Hub Schema 与验证测试所有；数据访问由窄 Port/Adapter 所有。旧合同不构成兼容承诺，兄弟项目 consumer 需要显式迁移。
