#!/usr/bin/env bash
# Start hub-api with host/port from config/settings.yaml (secrets from config/.env).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
HOST_PORT="$("${ROOT}/.venv/bin/python" -c "from hub.config import load_config; c=load_config(); print(c.api.host, c.api.port)")"
read -r HUB_HOST HUB_PORT <<<"$HOST_PORT"
exec "${ROOT}/.venv/bin/uvicorn" hub.main:app \
  --host "$HUB_HOST" \
  --port "$HUB_PORT" \
  --proxy-headers \
  --forwarded-allow-ips='*'
