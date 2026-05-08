# 开发环境（命令行启动）

在仓库根目录准备好 `.env`（可参考本目录 `.env.example`）。`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` 须与本目录 `livekit.yaml` 中的 `keys` 一致。

## 一键启动 / 停止（推荐）

```bash
cd deploy/dev
chmod +x run_all.sh
./run_all.sh          # 后台启动 LiveKit + Hub + Web + Admin
./run_all.sh status   # 查看状态
./run_all.sh stop     # 全部停止
```

日志：`logs/run_all_api.log`、`run_all_web.log`、`run_all_admin.log`（相对仓库根）。进程号：`logs/run_all.pids`。

各脚本在**启动成功后**会打印统一格式的访问地址（默认主机 `127.0.0.1`）。局域网调试可导出后再启动：

```bash
export DEV_URL_HOST=192.168.1.10   # 打印出的链接中的主机名
export HUB_PORT=8081               # 与 Hub 实际端口一致时用于文案
```

## 分进程启动（可选）

1. **LiveKit**（本目录）

   ```bash
   cd deploy/dev
   chmod +x run-livekit.sh
   ./run-livekit.sh
   ```

   后台：`./run-livekit.sh -d`；停止：`./run-livekit.sh stop`

2. **Hub API**

   ```bash
   cd deploy/dev
   chmod +x run-api.sh
   ./run-api.sh
   ```

3. **Web 客户端**（Next.js）

   ```bash
   cd deploy/dev/web
   chmod +x run-client.sh
   ./run-client.sh
   ```

4. **Admin**（Vite）

   ```bash
   cd deploy/dev/web
   chmod +x run-admin.sh
   ./run-admin.sh
   ```

## 端口

| 服务    | 端口  |
|---------|-------|
| LiveKit | 7880  |
| Hub API | 8081  |
| Web     | 3000  |
| Admin   | 5174  |

## 防火墙 / WebRTC（局域网）

若其它设备要连本机的 LiveKit，请在主机防火墙上放行 **7880/tcp**、**3478/udp**（若启用 TURN），以及 `livekit.yaml` 中 **rtc.port_range_start–end** 的 **UDP** 段（默认 50000–60000）。局域网场景下客户端应使用 **`ws://<本机局域网 IP>:7880`**，勿使用 `127.0.0.1`。

## Admin API 地址

默认 `http://localhost:8081/api/admin`。若需覆盖，在 `client/admin` 下设置 `VITE_ADMIN_API_BASE`。

## 健康检查

```bash
./healthcheck.sh
```
