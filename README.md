# eidolon-hub

Eidolon Hub - 统一设备接入层和 Agent 适配层。

## Quick Start

```bash
# Install dependencies
uv sync

# Run
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8081
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HUB_HOST` | `0.0.0.0` | HTTP bind host |
| `HUB_PORT` | `8081` | HTTP bind port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LIVEKIT_API_KEY` | — | LiveKit API key |
| `LIVEKIT_API_SECRET` | — | LiveKit API secret |
| `EIDOLON_LIVEKIT_URL` | — | LiveKit server URL |

## API Endpoints

### ESP32 Device

```
GET /api/esp32/config
Headers: X-Device-ID: <device-id>
Query: room_name, agent_mode (streaming|ptt)
```

### Web Client

```
GET /api/web/config
Query: room_name, participant_name, agent_mode (streaming|ptt)
```

## LAN Discovery (mDNS / Zeroconf)

Hub automatically announces itself on the local network via mDNS, allowing ESP32 devices and web clients to discover it without manual IP configuration.

### Service Details

- **Type**: `_eidolon-hub._tcp.local.`
- **Hostname**: `eidolon-hub.local`
- **Port**: `HUB_PORT` (default 8081)

### TXT Record Fields (RFC 6763)

| Key | Description |
|-----|-------------|
| `txtvers` | TXT schema version (`1`) |
| `version` | Hub software version |
| `api` | API major version (`v1`) |
| `livekit_url` | LiveKit server URL |
| `esp32_config` | ESP32 config endpoint path |
| `web_config` | Web config endpoint path |

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
  "http://eidolon-hub.local:8081/api/esp32/config?room_name=demo"
```

**ESP32 (ESP-IDF)**: Use the built-in mDNS API to query `_eidolon-hub._tcp.local.`, read the TXT record, then call the endpoint paths found in `esp32_config` and `livekit_url`.

## License

MIT
