# 测试推动的架构发现

1. mDNS advertiser 与 MQTT client 只有 start/stop 形似，能力并不相同；共同 `ConnectionConnector` Port 是伪抽象，已删除。
2. WAN bootstrap 用稳定 HTTPS URI 已足够；MQTT 的 QoS/长连接价值属于实际 Channel backend，不属于 Hub 注册。
3. 进程内 grant mailbox 在 Cloud 多实例无法恢复或定向；直接 Acquire request/reply 更短、更一致。
4. 后台 desired-state worker/SQL claim 把 Provider 资源策略带入 Hub。设备主动 Acquire 后，Hub 只保留授权与通用 assignment 门禁。
5. Channel active 不能定义设备在线；否则 Provider presence 会重新污染 Device Management。Session 与 Channel 双门禁由测试锁定。
6. Directory cache 必须 DB-first、启动恢复并周期对账；它只优化热读，不能承担授权或 fencing。
7. PostgreSQL 并发测试证明同一 device authority 只能由一个 Hub instance 获取；SQLite 只用于单节点 Local。
8. `eidolon_channel` 尚未实现新 Provider 契约，本轮没有修改兄弟项目，不能声称 Eidolon OS 整栈已切换。
