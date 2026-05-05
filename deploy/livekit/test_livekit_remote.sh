#!/usr/bin/env bash
#
# LiveKit Server 远程可用性测试
# 在本地运行，测试服务器 LiveKit 服务是否可从外网访问
#
# 用法:
#   ./test_livekit_remote.sh
#

set -euo pipefail

DOMAIN="${LIVEKIT_DOMAIN:-livekit-server.yangtzeailab.com}"
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }

echo ""
echo "LiveKit Server: https://$DOMAIN"
echo ""

FAILED=0

# DNS
if HOST_IP=$(dig +short "$DOMAIN" 2>/dev/null | tail -1) && [[ -n "$HOST_IP" ]]; then
    log_ok "DNS: $DOMAIN -> $HOST_IP"
else
    log_fail "DNS 解析失败"
    FAILED=1
fi

# HTTPS
if curl -sf --max-time 10 -I "https://$DOMAIN/" >/dev/null 2>&1; then
    log_ok "HTTPS 连接正常"
else
    log_fail "HTTPS 连接失败"
    FAILED=1
fi

# SSL 证书
if echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout 2>/dev/null; then
    SUBJECT=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null | cut -d'=' -f2-)
    log_ok "SSL 证书有效: $SUBJECT"
else
    log_fail "SSL 证书问题"
    FAILED=1
fi

# TURN UDP
if timeout 3 nc -zu "$HOST_IP" 3478 2>/dev/null; then
    log_ok "TURN UDP (3478) 开放"
else
    log_fail "TURN UDP (3478) 未开放"
    FAILED=1
fi

echo ""
if [[ $FAILED -eq 0 ]]; then
    echo "结果: LiveKit 服务正常"
else
    echo "结果: 存在问题，请检查防火墙"
fi
echo ""
