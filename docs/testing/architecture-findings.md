# 架构发现与反思（2026-08-01）

1. Hub 配置 Channel Profile 会让设备总线拥有传输/媒体策略。新边界只配置 Provider 契约地址，Provider 从 typed device context 独立产生 generic assignments。
2. Provider 放在注册关键路径会把外部资源故障升级为设备事实丢失。现在事实 durable-first，后台 desired-state reconcile 最终收敛。
3. desired revision 不能同时充当永久 Channel issuance ID。设备事实可能不变，但 lease 会过期；因此失败/补投复用 operation，终态/刷新使用确定性的 assignment generation。
4. Repository 不应判断 Channel 是否收敛。真实 SQL 与 Unit Fake 的差异证明 claim 只应处理互斥，业务收敛属于 Application。
5. opaque binding 既不能持久化，也不能靠 Hub 自身补发。Hub 重新调用 Provider 的幂等 operation 取得响应后中继；日志、Directory、Event Bus 和 SQL 只保存通用 metadata。
6. ambient `HTTP_PROXY` 会绕过明确配置并可能泄漏 Provider bearer token。Provider egress 禁止继承环境代理。
7. DB 是设备事实、授权、命令和 lease 的权威；内存只缓存允许短暂陈旧的 Directory 读路径。memory-first 异步落库不适合多实例或崩溃一致性。
8. Local/Cloud parity 已由同 artifact/OpenAPI/config-only 测试锁定；本机 PostgreSQL 18 round-trip/并发 claim 与 Mosquitto MQTT5 QoS1 已有真实证据。生产 TLS/ACL、网络分区和 rolling restart 仍无证据，不能从本机基础测试推导为生产稳定。
9. 删除必须同时检查生产 import、Composition、公共契约和所有权。只按 coverage 删除会误删发布的 Guard/Sense Schema，只按历史计划保留则会留下 Admin/SSE/EventLedger 等无消费者实现。
10. Source 删除不等于 artifact 删除。Setuptools 会复用旧 `build/lib`，首次 wheel 仍包含已删实现；清理精确生成目录后，clean wheel 才达到旧文件与旧依赖零匹配。发布门禁必须从干净构建目录执行。
11. Directory cache 对账不能修复错误的持久投影。Connection 在没有 Disconnect 时自然过期，旧 Directory 会永久保持 online；现在由独立 Application projection 周期重算，cache 仍只负责热读和跨实例 refresh。
12. Owner 是 Provider 签发连接凭据所需的安全上下文，不是通道策略。旧 desired hash 漏掉 owner transfer，导致 Provider 永远看不到归属变化；现在 `owner_id` 是必备 nullable Wire fact，并参与 operation revision。
13. 禁用 ambient proxy 仍不能让远端明文 HTTP 安全。Provider bearer credential 只允许经 HTTPS 发送；HTTP 仅限 IP loopback/localhost 的本地开发契约。
14. Device/Connection fact 与 durable Event Bus append 当前是两个顺序事务。事实写入后、事件写入前崩溃会留下不可重建的历史事件缺口；Directory/Channel 可由 reconcile 修复，历史 Event 不可以。外部 consumer 切换前需要数据库 Unit of Work/transactional outbox 和进程终止 fault test。
