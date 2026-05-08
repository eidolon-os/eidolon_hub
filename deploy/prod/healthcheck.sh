#!/usr/bin/env bash
# 检查本机 Hub 端口与（可选）compose 服务名
set -euo pipefail

PORT="${1:-8081}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

echo ""
echo "=== Hub API (http://127.0.0.1:$PORT) ==="
echo ""

if command -v curl >/dev/null 2>&1; then
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/docs" 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" == "200" ]; then
    log_info "API 文档可访问 (HTTP $HTTP_CODE)"
  else
    log_warn "API 文档不可访问 (HTTP $HTTP_CODE)"
  fi
else
  log_warn "未安装 curl"
fi

if command -v docker >/dev/null 2>&1; then
  echo ""
  echo "=== docker compose (本目录) ==="
  (cd "$SCRIPT_DIR" && docker compose ps 2>/dev/null) || log_warn "无法执行 docker compose ps"
fi

echo ""
