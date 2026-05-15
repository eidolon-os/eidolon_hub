# 开发环境（命令行启动）

## 快速开始

```bash
# 1) 在仓库根目录准备 .env
cp .env.example .env
#   - 必填：LIVEKIT_API_KEY / LIVEKIT_API_SECRET（须与本目录 livekit.yaml 的 keys 一致）
#   - ESP32 / 浏览器看到的 LiveKit URL：从三策略中任选一种填入（见下方表）

# 2) 一键启动
cd deploy/dev
chmod +x run_all.sh
./run_all.sh          # 后台启动 LiveKit + Hub + Web + Admin
./run_all.sh status   # 查看状态
./run_all.sh stop     # 全部停止
```

日志（相对仓库根）：`logs/run_all_api.log`、`logs/run_all_web.log`、`logs/run_all_admin.log`、`logs/livekit_server.log`。进程号：`logs/run_all.pids`、`logs/livekit_server.pid`。

打印的访问地址默认主机为 `127.0.0.1`。局域网调试可在启动前导出：

```bash
export DEV_URL_HOST=192.168.1.10   # 仅影响 _print_urls.sh 打印的链接文案
export HUB_PORT=8081               # 同上；与 Hub 实际端口保持一致即可
```

> 注意：`DEV_URL_HOST` / `HUB_PORT` 只影响**打印**，不改变实际监听端口。Hub 端口由 `.env` 里的 `HUB_PORT` 决定，LiveKit 端口由 `livekit.yaml` 决定。

## 配置文件关系

只有**一份** `.env` 真正生效 —— 位于仓库根目录，由 `hub/config.py` 在导入时通过 `python-dotenv` 的 `load_dotenv()` 加载（向上搜索 cwd）。`.env.example` 仅是模板，永远不会被代码读到。

```
仓库根 .env  ──load_dotenv()──▶  hub/config.py  ──▶  Hub API (uvicorn)
                                              ╲
                                               ╲──▶  GET /api/config  ──▶  ESP32 / Web 客户端
                                                          │  用 LIVEKIT_API_KEY/SECRET 签 JWT
                                                          │  用 EIDOLON_LIVEKIT_* 解析 ws URL

deploy/dev/livekit.yaml  ──▶  run-livekit.sh  ──▶  LiveKit server (7880)
        │
        └─ keys.eidolon 当前需 **手动** 与 .env 的 LIVEKIT_API_KEY / LIVEKIT_API_SECRET 保持一致

client/web/.env.local      ──▶  Next.js (3000)    NEXT_PUBLIC_LIVEKIT_URL / NEXT_PUBLIC_LIVEKIT_TOKEN_URL
client/admin/.env (可选)   ──▶  Vite     (5174)   VITE_ADMIN_API_BASE
```

关键约束：

- **`.env` 必须放仓库根**。`deploy/dev/` 下不再维护独立的 `.env.example`，仓库根 `.env.example` 是 dev / prod 通用模板。
- **`livekit.yaml` 的 `keys:` 与 `.env` 的 `LIVEKIT_API_KEY/SECRET` 是同一份凭据写两遍**，修改时要两边一起改（默认模板已对齐：`eidolon` / `eidolon-livekit-secret-2026`）。
- **前端两套 env 独立**：Next.js 需要 `NEXT_PUBLIC_*` 变量在 `client/web/.env.local`，Vite 需要 `VITE_*` 变量在 `client/admin/.env`。它们不会从仓库根 `.env` 读取。

## 核心环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIVEKIT_API_KEY` | `eidolon` | LiveKit API Key，与 `livekit.yaml` 的 keys 一致 |
| `LIVEKIT_API_SECRET` | `eidolon-livekit-secret-2026` | LiveKit API Secret，与 `livekit.yaml` 的 keys 一致 |
| `LIVEKIT_API_URL` | （空）| Hub 后端调 LiveKit **服务端管理 API** 的地址，**必须 `http://` 或 `https://`**，如 `http://127.0.0.1:7880` |
| `EIDOLON_LIVEKIT_URL` / `EIDOLON_LIVEKIT_IP` / `EIDOLON_LIVEKIT_PORT` | — | 下发给客户端的 LiveKit 信令地址（三策略，见下） |
| `HUB_HOST` / `HUB_PORT` | `0.0.0.0` / `8081` | Hub API 监听地址 / 端口 |
| `LOG_LEVEL` | `INFO` | Python 日志级别 |

完整变量列表（含 `MDNS_*`、`ADMIN_*`）见仓库根 `.env.example`。

## LiveKit 客户端 URL 三策略

`GET /api/config` 返回给 ESP32 / 浏览器的 `server_url`，优先级从上到下：

| 策略 | 用法 | 适用场景 |
|---|---|---|
| 1. 完整 URL | `EIDOLON_LIVEKIT_URL=wss://livekit.example.com` | 生产 / 已有完整地址 |
| 2. IP + 端口 | `EIDOLON_LIVEKIT_IP=192.168.x.x` + `EIDOLON_LIVEKIT_PORT=7880` | 局域网固定 IP |
| 3. auto | `EIDOLON_LIVEKIT_IP=auto`（或留空） | 跟随请求 Hub 的 Host；若是 loopback 则自动换成本机 LAN IPv4（见 [hub/config.py](../../hub/config.py)） |

## 分进程启动（可选）

1. **LiveKit**

   ```bash
   cd deploy/dev
   chmod +x run-livekit.sh
   ./run-livekit.sh           # 前台
   ./run-livekit.sh -d        # 后台
   ./run-livekit.sh stop      # 停止
   ```

2. **Hub API**

   ```bash
   cd deploy/dev
   chmod +x run-api.sh
   ./run-api.sh
   ```

3. **Web 客户端**（Next.js，端口 3000）

   ```bash
   cd deploy/dev/web
   chmod +x run-client.sh
   ./run-client.sh
   ```

4. **Admin**（Vite，端口 5174）

   ```bash
   cd deploy/dev/web
   chmod +x run-admin.sh
   ./run-admin.sh
   ```

## 端口

| 服务    | 端口  |
|---------|-------|
| LiveKit | 7880 (TCP/WS) + 3478 (UDP TURN) + 50000–60000 (UDP RTC) |
| Hub API | 8081 |
| Web     | 3000 |
| Admin   | 5174 |

## 修改端口要同步的位置

| 改谁 | 改哪些地方 |
|---|---|
| Hub API 端口 | `.env` 的 `HUB_PORT`；`export HUB_PORT=` 用于打印；`client/web/.env.local` 的 `NEXT_PUBLIC_LIVEKIT_TOKEN_URL`；`client/admin/.env` 的 `VITE_ADMIN_API_BASE` |
| LiveKit 端口 | `livekit.yaml` 的 `port`；`.env` 的 `EIDOLON_LIVEKIT_PORT` / `EIDOLON_LIVEKIT_URL`；`client/web/.env.local` 的 `NEXT_PUBLIC_LIVEKIT_URL` |

## 防火墙 / WebRTC（局域网）

若其它设备要连本机的 LiveKit，请在主机防火墙上放行 **7880/tcp**、**3478/udp**（若启用 TURN），以及 `livekit.yaml` 中 **rtc.port_range_start–end** 的 **UDP** 段（默认 50000–60000）。局域网场景下客户端应使用 **`ws://<本机局域网 IP>:7880`**，勿使用 `127.0.0.1`。

## Admin API 地址

默认 `http://localhost:8081/api/admin`。若需覆盖，在 `client/admin` 下设置 `VITE_ADMIN_API_BASE`。

## 健康检查

```bash
./healthcheck.sh
```
