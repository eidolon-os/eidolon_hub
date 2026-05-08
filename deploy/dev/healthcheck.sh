#!/usr/bin/env bash
# Hub API HTTP 检查（不依赖 systemd / Nginx）
set -euo pipefail

PORT="${1:-8081}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

echo ""
echo "=== Eidolon Hub API 健康检查 (http://127.0.0.1:$PORT) ==="
echo ""

if command -v curl >/dev/null 2>&1; then
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/docs" 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" == "200" ]; then
    log_info "API 文档可访问 (HTTP $HTTP_CODE)"
  else
    log_warn "API 文档不可访问 (HTTP $HTTP_CODE)"
  fi
else
  log_warn "未安装 curl，跳过 HTTP 检查"
fi

echo ""
echo "=== 完成 ==="
echo ""
