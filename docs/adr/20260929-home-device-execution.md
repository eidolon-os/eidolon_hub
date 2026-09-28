# 家庭设备执行归属 Hub

状态：实现中，运行时切换须按 Ops 的协调发布流程进行。

Hub 新增 `hub.smarthome`，负责家庭设备 Provider、命令校验、场景展开、deadline、幂等和 Provider 状态读取。该子域不导入 admission、Channel、Agent 或任何面板类型。`hub.composition.smarthome` 是唯一装配点。原生 Body 的身份准入、Channel handoff 和会话连接职责保持原边界；第三方家居端点不冒充原生 DeviceRef。

Data 继续保存注册、房间、名称、别名、场景和布局。SDK 原有设备/命令/回执契约保持不变。Agent 拥有对话状态和理解/提交决策，调用 Hub 内部 `POST /api/smarthome/v1/snapshot|execute`。Channel 移除对应 snapshot/execute HTTP 路由，只保留 voice result 投递以及面板输入/状态投影。

Provider 实现从 Channel 迁移，未复制双份。Channel 不依赖 Hub Python 包，只依赖内部 HTTP 与 SDK 契约。两个调用者使用各自已有的 HTTP 库；不建立通用 RPC 框架。唯一新增凭证 `EIDOLON_HUB_SMARTHOME_TOKEN` 由 Ops 生成并交付给 Hub/Agent/Channel。Data 的 Workspace 凭证从 Channel 移交 Hub，原有 Channel Provider 凭证权限不扩大。

面板复用已有快照与轮询机制：触屏执行后及语音结果到达时刷新，已有五秒轮询覆盖外部变化。发送面板消息不会阻塞 Hub 的执行锁或 Agent 的执行回执。首版使用全量快照进行状态投影，不增加事件总线或第二份持久设备状态。若后续测得五秒外部更新不满足体验，针对观测订阅接口演进；不先建立空泛事件框架。

## 已有状态与升级

虚拟设备状态的唯一文件迁至 `$EIDOLON_STATE_ROOT/hub/smarthome.sqlite3`，表结构不变。Hub 检出旧的 `channel/smarthome.sqlite3` 存在而新文件不存在时拒绝静默创建空库。协调切换需要停掉旧执行者、用 SQLite backup 保留旧库与 WAL 内容、校验后迁移，再启动新执行者；不能让两套执行者并存。暂不执行线上切换，以免尚未纳入发布流程的文件迁移丢失状态。

新内部凭证通过已有 Ops converge-inputs 机制补齐，不手工轮换其他密钥。部署 settings 跟随各组件源模板，旧凭证通过既有输入收敛撤回。回滚也必须协调 Provider 状态，不能只回滚代码指针。

## 有意保留的范围

目前只注册 VirtualProvider。执行去重仍为有界进程内缓存，未声称跨重启 exactly-once；真实供应商接入前需持久化提交记录与结果对账。现有 SDK succeeded 必须有完整 state，故云云自然语言 API 尚不能假装成该精确 Provider。新的接入能力和状态契约应另行明确，不由本次迁移猜测字段。

## 验证

执行侧覆盖 owner 隔离、并发去重、冲突、touch 作用域、deadline/unknown、部分成功、场景、注册失败、Provider 独立状态变化；Channel 覆盖真实固件 golden snapshot/envelope、同步与慢面板隔离。新增 import-linter 约束禁止执行核心依赖准入和呈现层。
