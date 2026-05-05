#!/usr/bin/env bash
#
# LiveKit Server 快速测试脚本
# 用法: ./test_livekit.sh
#

set -euo pipefail

LIVEKIT_URL="${LIVEKIT_URL:-wss://livekit-server.yangtzeailab.com}"
API_KEY="${LIVEKIT_API_KEY:-devkey}"
API_SECRET="${LIVEKIT_API_SECRET:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo ""
echo "=========================================="
echo "  LiveKit Server 快速测试"
echo "=========================================="
echo ""

# 测试 1: HTTP 健康检查
log_info "[1/4] 测试 HTTP 健康检查..."
if curl -sf --max-time 5 "http://127.0.0.1:7880/" >/dev/null 2>&1; then
    log_info "HTTP 服务正常"
elif curl -sf --max-time 5 "https://127.0.0.1:7880/" --insecure >/dev/null 2>&1; then
    log_info "HTTPS 服务正常"
else
    log_warn "HTTP/HTTPS 健康检查失败 (可能需要其他端点)"
fi

# 测试 2: WebSocket 连接
log_info "[2/4] 测试 WebSocket 连接..."
if command -v curl >/dev/null 2>&1; then
    WEBSOCKET_HOST=$(echo "$LIVEKIT_URL" | sed 's|wss://||;s|https://||;s|http://||' | cut -d':' -f1)
    if timeout 5 curl -s --max-time 5 "https://$WEBSOCKET_HOST/" >/dev/null 2>&1; then
        log_info "WebSocket 域名解析正常"
    else
        log_warn "WebSocket 域名可能未配置或 DNS 未生效"
    fi
fi

# 测试 3: Docker 容器状态
log_info "[3/4] 检查 Docker 容器..."
if docker ps --format '{{.Names}}:{{.Status}}' | grep -q "^livekit:Up"; then
    log_info "容器运行正常"
else
    log_error "容器未运行，请检查: docker ps -a | grep livekit"
fi

# 测试 4: API Secret 检查
log_info "[4/4] 检查 API Secret..."
if [[ -z "$API_SECRET" ]]; then
    log_warn "LIVEKIT_API_SECRET 未设置"
    echo ""
    echo "  从服务器获取 API Secret:"
    echo "    grep -A1 'devkey' deploy/livekit/livekit.yaml"
    echo ""
    echo "  然后设置:"
    echo "    export LIVEKIT_API_SECRET=<secret>"
    echo "    export LIVEKIT_URL=$LIVEKIT_URL"
    echo "    export LIVEKIT_API_KEY=$API_KEY"
    echo ""
else
    log_info "API Secret 已设置"
fi

echo ""
echo "=========================================="
echo "  端口检查"
echo "=========================================="
echo ""

# 检查必要端口
check_port() {
    local port=$1
    local name=$2
    if timeout 2 nc -z 127.0.0.1 "$port" 2>/dev/null; then
        log_info "$port/tcp ($name) 开放"
    else
        log_warn "$port/tcp ($name) 未开放"
    fi
}

check_udp() {
    local port=$1
    local name=$2
    if timeout 2 nc -zu 127.0.0.1 "$port" 2>/dev/null; then
        log_info "$port/udp ($name) 开放"
    else
        log_warn "$port/udp ($name) 未开放"
    fi
}

check_port 7880 "HTTP API"
check_port 7881 "WebRTC TCP"
check_port 5349 "TURN TLS"
check_udp  3478 "TURN UDP"

echo ""
echo "=========================================="
echo "  测试完成"
echo "=========================================="
echo ""
