#!/usr/bin/env bash
#
# LiveKit Server 可用性测试
# - 本地运行: 测试从外网访问服务器
# - 服务器运行: 测试本地服务
#
# 用法:
#   ./test_livekit.sh              # 自动检测
#   ./test_livekit.sh local        # 服务器本地测试
#   ./test_livekit.sh remote       # 远程测试 (指定域名)
#   DOMAIN=xxx ./test_livekit.sh   # 远程测试 (指定域名)
#

set -euo pipefail

MODE="${1:-auto}"
DOMAIN="${DOMAIN:-livekit-server.yangtzeailab.com}"
SERVER_IP="${SERVER_IP:-8.141.101.214}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }

# 检测是否在服务器上运行
is_server() {
    [[ -f /.dockerenv ]] || grep -q "docker\|lxc" /proc/1/cgroup 2>/dev/null || \
    [[ "$(hostname -I 2>/dev/null)" =~ ^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.) ]]
}

run_local_test() {
    echo ""
    echo "=== 服务器本地测试 ==="
    echo ""

    # 监听地址
    BIND_ADDR=$(grep "^bind:" /usr/local/app/eidolon-hub/deploy/livekit/livekit.yaml 2>/dev/null | awk '{print $2}' || echo "0.0.0.0:7880")
    log_info "LiveKit 监听: $BIND_ADDR"

    # HTTP
    if curl -sf --max-time 5 -I "http://127.0.0.1:7880/" >/dev/null 2>&1; then
        log_ok "HTTP (7880) 正常"
    else
        log_fail "HTTP (7880) 未响应"
    fi

    # UDP
    if nc -zu 127.0.0.1 3478 2>/dev/null; then
        log_ok "TURN UDP (3478) 正常"
    else
        log_fail "TURN UDP (3478) 未响应"
    fi

    # 进程
    if pgrep -x "livekit-server" >/dev/null; then
        log_ok "进程运行中"
    else
        log_fail "进程未运行"
    fi

    # 端口监听
    if ss -tlnp 2>/dev/null | grep -q ":7880\|:3478"; then
        log_ok "端口监听正常"
    else
        log_info "检查端口: ss -tlnp | grep -E '7880|3478'"
    fi
}

run_remote_test() {
    echo ""
    echo "=== 远程测试: $DOMAIN ==="
    echo ""

    FAILED=0

    # DNS
    if HOST_IP=$(dig +short "$DOMAIN" 2>/dev/null | tail -1) && [[ -n "$HOST_IP" ]]; then
        # 检查是否为公网 IP
        if [[ "$HOST_IP" =~ ^10\. ]] || [[ "$HOST_IP" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] || [[ "$HOST_IP" =~ ^192\.168\. ]]; then
            log_fail "DNS: $DOMAIN -> $HOST_IP (内网IP，请关闭代理)"
            FAILED=1
        else
            log_ok "DNS: $DOMAIN -> $HOST_IP"
        fi
    else
        log_fail "DNS 解析失败"
        FAILED=1
        return
    fi

    # HTTPS
    if curl -sf --max-time 10 -I "https://$DOMAIN/" >/dev/null 2>&1; then
        log_ok "HTTPS 连接正常"
    else
        log_fail "HTTPS 连接失败"
        FAILED=1
    fi

    # SSL
    if echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout 2>/dev/null; then
        SUBJECT=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null | cut -d'=' -f2-)
        log_ok "SSL 证书有效: $SUBJECT"
    else
        log_fail "SSL 证书问题"
        FAILED=1
    fi

    # TURN UDP
    if nc -zu -w 3 "$HOST_IP" 3478 2>/dev/null; then
        log_ok "TURN UDP (3478) 开放"
    else
        log_fail "TURN UDP (3478) 未开放"
        FAILED=1
    fi

    return $FAILED
}

# 主逻辑
case "$MODE" in
    local)
        run_local_test
        ;;
    remote)
        run_remote_test
        ;;
    auto)
        if is_server; then
            run_local_test
        else
            run_remote_test
        fi
        ;;
    *)
        echo "用法: $0 [local|remote]"
        echo "  local  - 服务器本地测试"
        echo "  remote - 远程测试 (需设置 DOMAIN)"
        echo "  auto   - 自动检测 (默认)"
        exit 1
        ;;
esac

echo ""
