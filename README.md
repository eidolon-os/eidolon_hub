# eidolon-hub

Eidolon Hub - LAN discovery + runtime connection broker.

Hub 只负责设备接入、可达性和跨会话控制面：mDNS 广播、`/api/config`、设备签名/批准/在线事实、LiveKit token 下发、room 名称分配、control room presence probe，以及可审计的 Hub command ack/result 历史。业务身份、`device_id -> agent_id` 绑定、tenant/user/template/memory resolve 都属于 `eidolon_admin`；语音会话体验和 voice room 生命周期属于 `eidolon_channel`。

## Quick Start

```bash
# Install dependencies
uv sync

# Run
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8082
```

## 部署与运行方式

- **本地开发（NATS + LiveKit + Hub + 全栈）**：由 [eidolon_admin](../eidolon_admin) 统一编排，见 `eidolon_admin/deploy/dev/run_all.sh`。端口在 `eidolon_admin/config/ports.yaml` 集中管理。首次配置 Hub： `./scripts/init-config.sh`（或 eidolon_admin Configs 面板从模板创建）。

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HUB_HOST` | `0.0.0.0` | HTTP bind host |
| `HUB_PORT` | `8082` | HTTP bind port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LIVEKIT_API_KEY` | — | LiveKit API key |
| `LIVEKIT_API_SECRET` | — | LiveKit API secret |
| `LIVEKIT_URL` | — | LiveKit **server HTTP API** URL for Hub (e.g. `http://127.0.0.1:7880`); if unset, falls back to `EIDOLON_LIVEKIT_URL` with `ws`→`http` |
| `EIDOLON_LIVEKIT_URL` | — | If set, full **client** WebSocket URL (`ws://…` / `wss://…`) returned in `GET /api/config` (highest priority) |
| `EIDOLON_LIVEKIT_IP` | — | If set and not `auto`, client URL host is this value; empty or `auto` → use request **Host** if non-loopback, else **LAN outbound IPv4** (never `127.0.0.1` for that fallback) |
| `EIDOLON_LIVEKIT_PORT` | `7880` | Client WebSocket port when URL is composed from IP or Host |
| `EIDOLON_LIVEKIT_SCHEME` | — | `ws` or `wss` when composing URL; if empty: explicit IP defaults to `ws`; auto-host uses `wss` when Hub request is `https`, else `ws` |

## API Endpoints

### Unified config (ESP32 + Web)

```
GET /api/config
Query:
  client_type: esp32 (default) | web
  room_name: optional for esp32 (auto if omitted); required for web
  user_id: required when client_type=web
  agent_mode: streaming|ptt  # LiveKit agent dispatch mode, not duplex capability
  Headers (esp32 only): X-Device-ID: <device-id>
  Headers (esp32/web):  X-Device-Interaction-Mode: half_duplex|full_duplex
```

ESP32 响应体与原先 `/api/esp32/config` 相同；Web 响应体与原先 `/api/web/config` 相同（`identity` + `accessToken`）。

下发给设备的 `config.server_url` 按顺序解析：`EIDOLON_LIVEKIT_URL`（完整）→ 否则 `EIDOLON_LIVEKIT_IP`（非 `auto`）+ 端口 → 否则用本次请求的 `Host` + 端口；若 `Host` 为 `localhost` / `127.0.0.1` 等回环地址（或 `livekit_ip=auto` 且等价场景），则改为本机**局域网出站 IPv4**（与 mDNS 注册同源探测），避免其它设备拿到不可达的 `127.0.0.1`。

## Boundary with Admin / Channel / LiveKit

- `eidolon_admin` owns business orchestration: `device_id -> agent_id` binding, tenant/user/agent/template/memory/voiceprint resolve, `/api/devices` combined views, and `/api/resolve/*`.
- `eidolon_channel` owns the voice session after a participant enters a voice room: participant metadata parsing, STT/VAD/EOT/LLM/TTS, turn policy, interrupt/ducking, `session_end`, idle teardown, and deleting the current voice room.
- LiveKit is shared infrastructure. `eidolon_admin` starts the local LiveKit server from `eidolon_admin/deploy/livekit/livekit.yaml`. Hub and Channel use the same server/key/secret, but with different roles:
  - Hub: token issuer, room/controller HTTP API caller, LAN-reachable client signaling URL provider.
  - Channel: LiveKit agent worker and voice room participant.
  - Device/Web: ordinary participants that receive Hub-signed token + signaling URL and never see key/secret.
- This project intentionally does not introduce a unified LiveKit Profile yet. Current config mapping is:
  - Hub: `livekit.api_url`, `livekit.api_key`, `livekit.api_secret`, `esp32.livekit_*`.
  - Channel: `core.livekit_url`, `core.api_key`, `core.api_secret`.
  - Server: `eidolon_admin/deploy/livekit/livekit.yaml`.

## LAN Discovery (mDNS / Zeroconf)

Hub automatically announces itself on the local network via mDNS, allowing ESP32 devices and web clients to discover it without manual IP configuration.

### Service Details

- **Type**: `_eidolon-hub._tcp.local.`
- **Hostname**: `eidolon-hub.local`
- **Port**: `HUB_PORT` (default 8082)

### TXT Record Fields (RFC 6763)

| Key | Description |
|-----|-------------|
| `txtvers` | TXT schema version (`1`) |
| `version` | Hub software version |
| `api` | API major version (`v1`) |
| `config_url` | Absolute URL for `GET /api/config` on this Hub |

### Usage

**macOS / Linux / Windows 10+**: The OS resolves `.local` names natively.

```bash
# Browse for services
dns-sd -B _eidolon-hub._tcp local.

# Resolve and get TXT record
dns-sd -L "Eidolon Hub" _eidolon-hub._tcp local.

# Ping the hub
ping eidolon-hub.local

# Access from another machine on LAN
curl -H "X-Device-ID: test-001" \
  "http://eidolon-hub.local:8082/api/config?room_name=demo"
```

**ESP32 (ESP-IDF)**: Use the built-in mDNS API to query `_eidolon-hub._tcp.local.`, read the TXT record, then call `config_url`.

## License

MIT
