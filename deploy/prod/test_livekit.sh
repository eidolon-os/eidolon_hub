#!/usr/bin/env bash
# LiveKit 可用性快速检查（本目录 livekit.yaml + 本机 7880）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIVEKIT_YAML="$SCRIPT_DIR/livekit.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }

echo ""
echo "=== LiveKit 本机探测 ==="
echo ""

if [[ -f "$LIVEKIT_YAML" ]]; then
  log_info "配置: $LIVEKIT_YAML"
else
  log_fail "缺少 $LIVEKIT_YAML"
  exit 1
fi

if curl -sf --max-time 5 -I "http://127.0.0.1:7880/" >/dev/null 2>&1; then
  log_ok "HTTP 7880 响应"
else
  log_fail "HTTP 7880 无响应（LiveKit 是否已启动？）"
fi

if command -v nc >/dev/null 2>&1; then
  if nc -zu -w 2 127.0.0.1 3478 2>/dev/null; then
    log_ok "UDP 3478 可探测"
  else
    log_fail "UDP 3478 无响应（若未启用 TURN 可忽略）"
  fi
else
  log_info "未安装 nc，跳过 UDP 3478"
fi

echo ""
