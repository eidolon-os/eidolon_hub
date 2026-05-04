#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检查 root 权限
if [[ "$(id -u)" -ne 0 ]]; then
    log_error "请使用 root 用户运行 (sudo)"
    exit 1
fi

echo ""
echo "=========================================="
echo "  LiveKit Server 部署脚本"
echo "=========================================="
echo ""

# 1. 检查并安装 Docker
log_info "检查 Docker 环境..."
if ! command -v docker >/dev/null 2>&1; then
    log_info "安装 Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    log_info "Docker 已安装: $(docker --version)"
fi

# 2. 检查并安装 Docker Compose
if ! command -v docker compose >/dev/null 2>&1; then
    if command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD="docker-compose"
    else
        log_error "Docker Compose 未安装"
        exit 1
    fi
else
    COMPOSE_CMD="docker compose"
fi

# 3. 检查并拉取 LiveKit 镜像 (使用国内镜像加速)
IMAGE="livekit/livekit-server:v1.11.0"
MIRROR_IMAGE="docker.m.daocloud.io/livekit/livekit-server:v1.11.0"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    log_info "拉取 LiveKit 镜像 (使用 DaoCloud 加速)..."
    if docker pull "$MIRROR_IMAGE" 2>/dev/null; then
        docker tag "$MIRROR_IMAGE" "$IMAGE"
        log_info "镜像拉取成功"
    else
        log_info "使用默认源拉取镜像..."
        docker pull "$IMAGE"
    fi
else
    log_info "LiveKit 镜像已存在: $IMAGE"
fi
NGINX_SSL_DIR="/etc/nginx/ssl/yangtzeailab.com"
if [[ ! -f "$NGINX_SSL_DIR/fullchain.pem" ]]; then
    log_error "SSL 证书未找到: $NGINX_SSL_DIR/fullchain.pem"
    log_error "请先运行 ../nginx/install.sh 安装 SSL 证书"
    exit 1
fi
log_info "SSL 证书已准备: $NGINX_SSL_DIR"

# 5. 生成或使用 API Key
if [[ ! -f "$SCRIPT_DIR/livekit.yaml" ]]; then
    log_info "创建 LiveKit 配置文件..."
    
    # 生成随机密钥
    API_KEY="devkey"
    API_SECRET=$(openssl rand -hex 16)
    
    # 创建配置
    cat > "$SCRIPT_DIR/livekit.yaml" << EOF
port: 7880
bind_addresses:
  - 127.0.0.1

keys:
  $API_KEY: $API_SECRET

turn:
  enabled: true
  domain: livekit-turn.eidolon.yangtzeailab.com
  cert_file: /etc/certs/fullchain.pem
  key_file: /etc/certs/privkey.pem
  tls_port: 5349
  udp_port: 3478

rtc:
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
  tcp_port: 7881

logging:
  level: info
  json: false
EOF
    
    log_info "API Secret 已生成，请保存以下信息:"
    echo ""
    echo "  API Key:    $API_KEY"
    echo "  API Secret: $API_SECRET"
    echo ""
    echo "  请将以下内容添加到环境变量或 .env 文件:"
    echo "  export LIVEKIT_API_KEY=$API_KEY"
    echo "  export LIVEKIT_API_SECRET=$API_SECRET"
    echo ""
else
    log_info "使用已有配置文件"
fi

# 6. 更新 docker-compose.yaml 中的证书路径
log_info "更新 Docker Compose 配置..."
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yaml"

# 检查容器是否已存在并停止
if docker ps -a --format '{{.Names}}' | grep -q "^livekit$"; then
    log_warn "LiveKit 容器已存在，正在停止..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" down || true
fi

# 7. 启动 LiveKit
log_info "启动 LiveKit Server..."
cd "$SCRIPT_DIR"
$COMPOSE_CMD up -d

# 8. 等待并检查状态
log_info "等待服务启动..."
sleep 3

echo ""
echo "=========================================="
echo "  服务状态"
echo "=========================================="
$COMPOSE_CMD ps

# 9. 检查日志
echo ""
echo "=========================================="
echo "  最近日志"
echo "=========================================="
$COMPOSE_CMD logs --tail 20

# 10. 防火墙提示
echo ""
echo "=========================================="
echo "  部署完成!"
echo "=========================================="
echo ""
echo "  连接信息:"
echo "    WebSocket:  wss://livekit.eidolon.yangtzeailab.com"
echo "    TURN TLS:   livekit-turn.eidolon.yangtzeailab.com:5349"
echo "    TURN UDP:   <服务器IP>:3478"
echo ""
echo "  需要开放的防火墙端口 (云服务器安全组 + 本地 UFW):"
echo ""
echo "  === 必需 ==="
echo "    443/tcp         - HTTPS + WebSocket"
echo "    3478/udp        - TURN 中继"
echo "    50000-60000/udp - WebRTC 媒体流"
echo ""
echo "  === 可选 (建议也开，降低连接失败率) ==="
echo "    5349/tcp        - TURN TLS"
echo "    7881/tcp        - WebRTC TCP 回退"
echo ""
echo "  云服务器安全组 + 本地防火墙开放以上端口即可。"
echo ""
echo "  常用命令:"
echo "    查看日志: docker compose -f $COMPOSE_FILE logs -f"
echo "    停止服务: docker compose -f $COMPOSE_FILE down"
echo "    重启服务: docker compose -f $COMPOSE_FILE restart"
echo "=========================================="
echo ""

# 11. 验证服务
log_info "验证服务健康状态..."
sleep 2
if curl -sf http://127.0.0.1:7880/ >/dev/null 2>&1 || \
   curl -sf https://127.0.0.1:7880/ --insecure >/dev/null 2>&1; then
    log_info "LiveKit Server 运行正常"
else
    log_warn "LiveKit Server 可能未正常启动，请检查日志"
fi
