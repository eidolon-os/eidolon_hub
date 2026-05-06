#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

echo ""
echo "=========================================="
echo "  Eidolon Web Client 部署脚本"
echo "=========================================="
echo ""

# --------------------------------------------------
# 0. 前置检查
# --------------------------------------------------
log_step "0. 前置检查..."

if [[ "$(id -u)" -ne 0 ]]; then
    log_error "请使用 root 用户运行 (sudo)"
    exit 1
fi

if ! command -v node >/dev/null 2>&1; then
    log_error "node 未安装"
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    log_error "npm 未安装"
    exit 1
fi

log_info "node: $(node --version)"
log_info "npm: $(npm --version)"

# --------------------------------------------------
# 1. 获取项目路径
# --------------------------------------------------
log_step "1. 确定项目路径..."

PROJECT_ROOT="$SCRIPT_DIR"
while [[ "$PROJECT_ROOT" != "/" ]]; do
    if [[ -f "$PROJECT_ROOT/pyproject.toml" ]]; then
        break
    fi
    PROJECT_ROOT="$(dirname "$PROJECT_ROOT")"
done
if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
    log_error "未找到 pyproject.toml，无法确定项目根目录"
    exit 1
fi

WEB_DIR="$PROJECT_ROOT/client/web"
if [ ! -d "$WEB_DIR" ]; then
    log_error "Web 目录不存在: $WEB_DIR"
    exit 1
fi

SERVICE_NAME="eidolon-web"
log_info "项目路径: $PROJECT_ROOT"
log_info "Web 目录: $WEB_DIR"

# --------------------------------------------------
# 2. 安装依赖并构建
# --------------------------------------------------
log_step "2. 构建 Next.js 前端..."

cd "$WEB_DIR"
npm install
npm run build

# --------------------------------------------------
# 3. 部署 systemd 服务
# --------------------------------------------------
log_step "3. 创建 systemd 服务..."

NODE_BIN=$(which node)

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Eidolon Web Client (Next.js)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$WEB_DIR
ExecStart=$NODE_BIN $WEB_DIR/node_modules/.bin/next start --port 3000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment="NODE_ENV=production"
Environment="PORT=3000"

ProtectSystem=full
ProtectHome=yes
ReadWritePaths=$WEB_DIR
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "/etc/systemd/system/${SERVICE_NAME}.service"

log_info "重载 systemd..."
systemctl daemon-reload

if systemctl is-active --quiet "$SERVICE_NAME"; then
    log_info "重启服务..."
    systemctl restart "$SERVICE_NAME"
else
    log_info "启用并启动服务..."
    systemctl enable --now "$SERVICE_NAME"
fi

sleep 2

# --------------------------------------------------
# 4. 完成
# --------------------------------------------------
echo ""
echo "=========================================="
echo "  部署完成!"
echo "=========================================="
echo ""
echo "  访问地址: https://eidolon-hub.yangtzeailab.com"
echo ""
echo "  常用命令:"
echo "    查看状态: systemctl status $SERVICE_NAME"
echo "    查看日志: journalctl -u $SERVICE_NAME -f"
echo "    重启服务: systemctl restart $SERVICE_NAME"
echo "    停止服务: systemctl stop $SERVICE_NAME"
echo ""
echo "=========================================="
echo ""

systemctl status "$SERVICE_NAME" --no-pager || true
