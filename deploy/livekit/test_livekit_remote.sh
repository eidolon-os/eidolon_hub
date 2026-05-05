#!/usr/bin/env bash
#
# LiveKit Server 外网可用性测试
# 在本地机器运行，测试服务器是否可以从外网访问
#
# 用法:
#   1. 先从服务器获取 API Secret:
#      ssh root@<server> "grep -A1 'devkey' /usr/local/app/eidolon-hub/deploy/livekit/livekit.yaml"
#
#   2. 设置环境变量并运行:
#      export LIVEKIT_API_SECRET=<secret>
#      ./test_livekit_remote.sh
#

set -euo pipefail

LIVEKIT_URL="${LIVEKIT_URL:-wss://livekit-server.yangtzeailab.com}"
API_KEY="${LIVEKIT_API_KEY:-devkey}"
API_SECRET="${LIVEKIT_API_SECRET:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[TEST]${NC} $1"; }

DOMAIN=$(echo "$LIVEKIT_URL" | sed 's|wss://||;s|https://||;s|http://||' | cut -d':' -f1)

echo ""
echo "=========================================="
echo "  LiveKit Server 外网可用性测试"
echo "=========================================="
echo ""
echo "  目标: $LIVEKIT_URL"
echo "  域名: $DOMAIN"
echo ""

# 测试 1: DNS 解析
log_step "[1/7] 测试 DNS 解析..."
if HOST_IP=$(dig +short "$DOMAIN" 2>/dev/null | tail -1) && [[ -n "$HOST_IP" ]]; then
    log_info "DNS 解析成功: $DOMAIN -> $HOST_IP"
    SERVER_IP="$HOST_IP"
else
    log_error "DNS 解析失败: $DOMAIN"
    exit 1
fi

# 测试 2: HTTPS 连接
log_step "[2/7] 测试 HTTPS 连接..."
if curl -sf --max-time 10 -I "https://$DOMAIN/" >/dev/null 2>&1; then
    HTTPS_STATUS=$(curl -sf --max-time 10 -I "https://$DOMAIN/" 2>/dev/null | head -1)
    log_info "HTTPS 连接成功: $HTTPS_STATUS"
else
    log_error "HTTPS 连接失败"
    exit 1
fi

# 测试 3: SSL 证书
log_step "[3/7] 测试 SSL 证书..."
CERT=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout -subject -dates 2>/dev/null)
if [[ -n "$CERT" ]]; then
    echo "$CERT" | while read line; do
        echo "    $line"
    done
else
    log_error "SSL 证书获取失败"
fi

# 测试 4: WebSocket (通过 curl)
log_step "[4/7] 测试 WebSocket 端点..."
WS_RESP=$(curl -sf --max-time 10 -X GET "https://$DOMAIN/" -H "Upgrade: websocket" -H "Connection: Upgrade" 2>/dev/null || true)
if [[ -n "$WS_RESP" ]]; then
    log_info "WebSocket 端点响应正常"
else
    log_warn "WebSocket 端点未返回有效响应 (可能正常，需客户端测试)"
fi

# 测试 5: LiveKit HTTP API
log_step "[5/7] 测试 LiveKit HTTP API (房间列表)..."
API_RESP=$(curl -sf --max-time 10 -u "$API_KEY:$API_SECRET" "https://$DOMAIN/v2/rooms" 2>/dev/null || true)
if [[ -n "$API_RESP" ]]; then
    log_info "API 响应: $API_RESP"
else
    log_warn "API 未响应 (可能需要认证或端点配置)"
fi

# 测试 6: TURN UDP 端口
log_step "[6/7] 测试 TURN UDP 端口 (3478)..."
if timeout 5 nc -zu "$SERVER_IP" 3478 2>/dev/null; then
    log_info "UDP 3478 端口开放"
else
    log_warn "UDP 3478 端口可能未开放或被防火墙拦截"
fi

# 测试 7: Token 生成测试 (需要 API Secret)
echo ""
log_step "[7/7] Token 生成测试..."
if [[ -z "$API_SECRET" ]]; then
    log_warn "未设置 LIVEKIT_API_SECRET，跳过 Token 测试"
    echo ""
    echo "  获取 API Secret:"
    echo "    ssh root@<server> \"grep -A1 'devkey' /usr/local/app/eidolon-hub/deploy/livekit/livekit.yaml\""
    echo ""
    echo "  然后运行:"
    echo "    export LIVEKIT_API_SECRET=<secret>"
    echo "    $0"
else
    log_info "API Secret 已设置，可以生成 Token"
    # 如果有 python 和 livekit-api，可以测试 token 生成
    if command -v python3 >/dev/null 2>&1; then
        if python3 -c "from livekit import api" 2>/dev/null; then
            log_info "使用 Python 生成测试 Token..."
            python3 << EOF
import os
import asyncio
from livekit import api

async def gen_token():
    token = await api.create_token(
        api_key="$API_KEY",
        api_secret="$API_SECRET",
        identity="remote-test-user",
        name="Remote Test",
        room="test-room",
    )
    print(f"    Token: {token[:80]}...")
    
asyncio.run(gen_token())
EOF
        else
            log_info "未安装 livekit-api，跳过 Token 生成"
        fi
    fi
fi

echo ""
echo "=========================================="
echo "  测试完成"
echo "=========================================="
echo ""
echo "  在线 JWT 验证: https://jwt.io/"
echo "  LiveKit 控制台: https://cloud.livekit.io/"
echo ""
echo "  建议测试步骤:"
echo "  1. 用浏览器访问: https://$DOMAIN"
echo "  2. 或使用 LiveKit 官方示例: https://docs.livekit.io/realtime/"
echo ""
