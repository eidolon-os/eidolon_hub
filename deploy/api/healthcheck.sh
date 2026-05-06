#!/usr/bin/env bash
set -euo pipefail

APP_NAME="eidolon-hub-api"
PORT="${1:-8081}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

echo ""
echo "=========================================="
echo "  Eidolon Hub API 健康检查"
echo "=========================================="
echo ""

# 检查 systemd 服务
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    log_info "systemd 服务运行中: $APP_NAME"
    echo ""
else
    log_warn "systemd 服务未运行: $APP_NAME"
    echo ""
fi

# 检查端口
if command -v curl >/dev/null 2>&1; then
    log_info "检查 HTTP 端点..."
    echo ""

    # 检查根路径
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" == "200" ] || [ "$HTTP_CODE" == "307" ] || [ "$HTTP_CODE" == "404" ]; then
        log_info "✓ 服务响应正常 (HTTP $HTTP_CODE)"
    else
        log_warn "✗ 服务未响应 (HTTP $HTTP_CODE)"
    fi

    # 检查 docs 端点
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/docs" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" == "200" ]; then
        log_info "✓ API 文档可访问 (HTTP $HTTP_CODE)"
    else
        log_warn "✗ API 文档不可访问 (HTTP $HTTP_CODE)"
    fi

    echo ""
fi

# 检查 Nginx 反向代理
NGINX_CONF="/etc/nginx/conf.d/eidolon-hub-api.yangtzeailab.com.conf"
if [ -f "$NGINX_CONF" ]; then
    log_info "✓ Nginx 配置存在"
    if systemctl is-active --quiet nginx 2>/dev/null; then
        log_info "✓ Nginx 服务运行中"
    fi
else
    log_warn "✗ Nginx 配置不存在"
fi

echo ""
echo "=========================================="
echo "  检查完成"
echo "=========================================="
echo ""
echo "  访问地址:"
echo "    本地:     http://127.0.0.1:$PORT/docs"
echo "    公网:     https://eidolon-hub-api.yangtzeailab.com/docs"
echo ""
echo "  日志命令:"
echo "    journalctl -u $APP_NAME -f"
echo ""
echo "=========================================="
