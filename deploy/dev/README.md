# 开发环境（命令行启动）

## 快速开始

```bash
# 1) 准备 config/settings.yaml + config/.env
./deploy/dev/init.sh
#   - settings.yaml：api / logging / livekit.api_url / esp32 / mdns / admin_probe
#   - .env：仅 LIVEKIT_API_KEY / LIVEKIT_API_SECRET（须与 livekit.yaml 的 keys 一致）

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
export HUB_PORT=8082               # 可选；与 settings.yaml 的 api.port 一致，仅影响打印
```

> 注意：`DEV_URL_HOST` / `HUB_PORT` 只影响**打印**。Hub 监听地址/端口由 `config/settings.yaml` 的 `api.host` / `api.port` 决定；LiveKit 端口由 `livekit.yaml` 决定。

## 配置文件关系

```
config/settings.yaml  ──▶  hub/config.py  ──▶  Hub API (uvicorn)
config/.env (密钥)     ──load_dotenv()──▶       LIVEKIT_API_KEY / LIVEKIT_API_SECRET
                                              ╲
                                               ╲──▶  GET /api/config  ──▶  ESP32 / Web
                                                          │  JWT 用 .env 密钥
                                                          │  ws URL 用 settings.yaml 的 esp32.*

deploy/dev/livekit.yaml  ──▶  run-livekit.sh  ──▶  LiveKit server (7880)
        │
        └─ keys 须与 config/.env 的 LIVEKIT_API_KEY / LIVEKIT_API_SECRET 一致

client/web/.env.local      ──▶  Next.js (3000)    NEXT_PUBLIC_*
client/admin/.env (可选)   ──▶  Vite     (5174)   VITE_ADMIN_API_BASE
```

关键约束：

- **`config/settings.yaml` 必填**（缺失则启动报错）；`config/settings.example.yaml` 仅作模板。
- **`config/.env` 只放密钥**；业务项（端口、mDNS、Admin 探测、esp32 URL 策略等）一律写在 YAML。
- **前端 env 独立**：Next / Vite 各自 `.env.local`，不读取 hub 的 `config/.env`。

## 核心配置（settings.yaml）

| 段 | 字段 | 说明 |
|---|---|---|
| `api` | `host`, `port` | Hub API 监听 |
| `logging` | `level` | Python 日志级别 |
| `livekit` | `api_url` | Hub 调 LiveKit **管理 API**（`http://` 或 `https://`） |
| `esp32` | `livekit_url` / `livekit_ip` / `livekit_port` / `livekit_scheme` | 下发给客户端的信令 URL（三策略，见下） |
| `mdns` | `service_type`, `service_name`, … | mDNS 发现 |
| `admin_probe` | `enabled`, `interval_seconds`, … | Admin 在线探测 |

`config/.env` 仅：`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`。完整示例见 `config/settings.example.yaml`。

## LiveKit 客户端 URL 三策略

`GET /api/config` 返回给 ESP32 / 浏览器的 `server_url`，优先级从上到下：

| 策略 | 用法 | 适用场景 |
|---|---|---|
| 1. 完整 URL | `esp32.livekit_url=wss://livekit.example.com` | 生产 / 已有完整地址 |
| 2. IP + 端口 | `esp32.livekit_ip=192.168.x.x` + `livekit_port` | 局域网固定 IP |
| 3. auto | `esp32.livekit_ip=auto`（或留空） | 跟随请求 Hub 的 Host；loopback 时换 LAN IPv4（见 [hub/config.py](../../hub/config.py)） |

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
| Hub API | 8082（`config/settings.yaml` → `api.port`） |
| Web     | 3000 |
| Admin   | 5174 |

## 修改端口要同步的位置

| 改谁 | 改哪些地方 |
|---|---|
| Hub API 端口 | `config/settings.yaml` 的 `api.port`；`export HUB_PORT=` 仅用于打印；前端 `NEXT_PUBLIC_*` / `VITE_ADMIN_API_BASE` |
| LiveKit 端口 | `livekit.yaml` 的 `port`；`settings.yaml` 的 `esp32.livekit_port`；`client/web/.env.local` 的 `NEXT_PUBLIC_LIVEKIT_URL` |

## 防火墙 / WebRTC（局域网）

若其它设备要连本机的 LiveKit，请在主机防火墙上放行 **7880/tcp**、**3478/udp**（若启用 TURN），以及 `livekit.yaml` 中 **rtc.port_range_start–end** 的 **UDP** 段（默认 50000–60000）。局域网场景下客户端应使用 **`ws://<本机局域网 IP>:7880`**，勿使用 `127.0.0.1`。

## Admin API 地址

默认 `http://localhost:8081/api/admin`。若需覆盖，在 `client/admin` 下设置 `VITE_ADMIN_API_BASE`。

## 健康检查

```bash
./healthcheck.sh
```
