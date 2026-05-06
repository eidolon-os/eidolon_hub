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

APP_NAME="eidolon-web"
log_info "项目路径: $PROJECT_ROOT"
log_info "Web 目录: $WEB_DIR"

# --------------------------------------------------
# 2. 配置环境变量
# --------------------------------------------------
log_step "2. 配置环境变量..."

ENV_FILE="$WEB_DIR/.env.local"
ENV_EXAMPLE="$WEB_DIR/.env.local.example"

if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        log_info "复制环境变量模板..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        log_warn "请编辑 $ENV_FILE 配置实际的环境变量"
    else
        log_warn "未找到环境变量文件: $ENV_EXAMPLE"
    fi
else
    log_info "使用已有环境变量: $ENV_FILE"
fi

# --------------------------------------------------
# 3. 安装依赖并构建
# --------------------------------------------------
log_step "3. 构建 Next.js 前端..."

cd "$WEB_DIR"
npm install
npm run build

# --------------------------------------------------
# 4. 部署 systemd 服务
# --------------------------------------------------
log_step "4. 创建 systemd 服务..."

SERVICE_FILE="$SCRIPT_DIR/eidolon-web.service"
SERVICE_TARGET="/etc/systemd/system/${APP_NAME}.service"

if [ ! -f "$SERVICE_FILE" ]; then
    log_error "未找到 systemd 服务文件: $SERVICE_FILE"
    exit 1
fi

# 移除旧文件，创建软链接
rm -f "$SERVICE_TARGET"
ln -s "$SERVICE_FILE" "$SERVICE_TARGET"
chown root:root "$SERVICE_TARGET"
chmod 644 "$SERVICE_TARGET"

log_info "重载 systemd..."
systemctl daemon-reload

if systemctl is-active --quiet "$APP_NAME"; then
    log_info "重启服务..."
    systemctl restart "$APP_NAME"
else
    log_info "启用并启动服务..."
    systemctl enable --now "$APP_NAME"
fi

sleep 2

# --------------------------------------------------
# 5. 检查 Nginx SSL 证书
# --------------------------------------------------
log_step "5. 检查 SSL 证书..."

SSL_DIR="/etc/nginx/ssl/yangtzeailab.com"
if [ ! -f "$SSL_DIR/fullchain.pem" ] || [ ! -f "$SSL_DIR/privkey.pem" ]; then
    log_warn "SSL 证书未找到: $SSL_DIR"
    log_warn "请先运行 ../nginx/install.sh 安装 SSL 证书"
fi

# --------------------------------------------------
# 6. 部署 Nginx 反向代理
# --------------------------------------------------
log_step "6. 部署 Nginx 反向代理..."

NGINX_CONF="$SCRIPT_DIR/eidolon-hub.yangtzeailab.com.conf"
NGINX_TARGET="/etc/nginx/conf.d/eidolon-hub.yangtzeailab.com.conf"

if [ ! -f "$NGINX_CONF" ]; then
    log_error "未找到 Nginx 配置文件: $NGINX_CONF"
    exit 1
fi

# 移除旧配置，创建软链接
rm -f "$NGINX_TARGET"
ln -s "$NGINX_CONF" "$NGINX_TARGET"

log_info "测试 Nginx 配置..."
nginx -t && log_info "Nginx 配置测试通过" || { log_error "Nginx 配置测试失败"; exit 1; }

log_info "重载 Nginx..."
systemctl reload nginx || nginx -s reload

# --------------------------------------------------
# 7. 完成
# --------------------------------------------------
echo ""
echo "=========================================="
echo "  部署完成!"
echo "=========================================="
echo ""
echo "  访问地址: https://eidolon-hub.yangtzeailab.com"
echo ""
echo "  常用命令:"
echo "    查看状态: systemctl status $APP_NAME"
echo "    查看日志: journalctl -u $APP_NAME -f"
echo "    重启服务: systemctl restart $APP_NAME"
echo "    停止服务: systemctl stop $APP_NAME"
echo ""
echo "=========================================="
echo ""

systemctl status "$APP_NAME" --no-pager || true
