# Hub 当前代码清单

| 层 | 责任 | 当前核心模块 |
|---|---|---|
| Domain | 纯领域值与状态机 | devices、sessions、channels、commands |
| Application | 用例与投影 | enroll/register/renew/close、approve/revoke、acquire channels、command/envelope、device directory |
| Ports | 窄依赖接口 | repositories、identity、channels、event bus |
| Contracts | Wire source 与显式 mapper | session/device/channel JSON Schema、Pydantic bindings、generated shapes |
| Adapters | 技术实现 | Zeroconf advertiser、Provider HTTP/Data bridge、SQLite/PostgreSQL、P-256/JWT、OpenTelemetry |
| Interfaces | 发布边界 | Device Access、Device Management、Provider Gateway HTTP routers |
| Composition | 唯一组装点 | config、resources、device_access、channel_control、management、app |

已删除的生产概念：transport Connection Connector、Hub MQTT、signaling ref/mailbox、Provider desired-state sync/worker/SQL claim、Hub 端 URI/DNS resolver，以及旧 Connection schemas/tests。
