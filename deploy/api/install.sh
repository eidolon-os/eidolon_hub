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
echo "  Eidolon Hub API 一键部署脚本"
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

if ! command -v uv >/dev/null 2>&1; then
    log_error "uv 未安装，请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

UV_INDEX_URL="${UV_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

log_info "uv: $(uv --version)"
log_info "pip 镜像: $UV_INDEX_URL"

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
APP_NAME="eidolon-hub-api"

log_info "项目路径: $PROJECT_ROOT"

# --------------------------------------------------
# 2. 创建 venv 并安装依赖
# --------------------------------------------------
log_step "2. 创建虚拟环境并安装依赖..."

VENV_DIR="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
    log_info "创建虚拟环境 (.venv)..."
    uv venv "$VENV_DIR" --python 3.12
else
    log_info "虚拟环境已存在: $VENV_DIR"
fi

log_info "安装项目依赖..."
uv pip install -e "$PROJECT_ROOT" --python "$VENV_DIR/bin/python" --index-url "$UV_INDEX_URL"

# --------------------------------------------------
# 3. 确保运行时目录
# --------------------------------------------------
log_step "3. 创建运行时目录..."

mkdir -p "$PROJECT_ROOT/data"
mkdir -p "$PROJECT_ROOT/logs"

# --------------------------------------------------
# 4. 复制并加载环境变量
# --------------------------------------------------
log_step "4. 配置环境变量..."

ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$SCRIPT_DIR/env.api.example"

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

# 加载 .env 到环境变量
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

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
# 6. 部署 systemd 服务
# --------------------------------------------------
log_step "6. 创建 systemd 服务..."

SERVICE_FILE="$SCRIPT_DIR/eidolon-hub-api.service"
SERVICE_TARGET="/etc/systemd/system/eidolon-hub-api.service"

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
# 7. 检查 Nginx 配置
# --------------------------------------------------
log_step "7. 检查 Nginx 反向代理配置..."

NGINX_CONF="/etc/nginx/conf.d/eidolon-hub-api.yangtzeailab.com.conf"

if [ -f "$NGINX_CONF" ]; then
    log_info "Nginx 配置已存在: $NGINX_CONF"
    log_info "测试 Nginx 配置..."
    nginx -t && log_info "Nginx 配置测试通过" || log_warn "Nginx 配置测试失败"
    log_info "重载 Nginx..."
    systemctl reload nginx || nginx -s reload
else
    log_warn "Nginx 反向代理配置未找到"
    log_warn "请确保存在: $NGINX_CONF"
fi

# --------------------------------------------------
# 8. 验证服务
# --------------------------------------------------
log_step "8. 验证服务状态..."

sleep 2

API_PORT=8081
if command -v curl >/dev/null 2>&1; then
    if curl -sf "http://127.0.0.1:$API_PORT/docs" >/dev/null 2>&1; then
        log_info "API 服务运行正常!"
    else
        log_warn "API 服务可能未正常启动，请检查日志"
    fi
fi

# --------------------------------------------------
# 9. 完成
# --------------------------------------------------
echo ""
echo "=========================================="
echo "  部署完成!"
echo "=========================================="
echo ""
echo "  访问地址:"
echo "    API 文档: https://eidolon-hub-api.yangtzeailab.com/docs"
echo "    ReDoc:    https://eidolon-hub-api.yangtzeailab.com/redoc"
echo ""
echo "  本地端口: 127.0.0.1:$API_PORT"
echo ""
echo "  常用命令:"
echo "    查看状态: systemctl status $APP_NAME"
echo "    查看日志: journalctl -u $APP_NAME -f"
echo "    重启服务: systemctl restart $APP_NAME"
echo "    停止服务: systemctl stop $APP_NAME"
echo ""
echo "  需要开放的防火墙端口:"
echo "    $API_PORT/tcp - API 服务 (仅本地 Nginx 访问)"
echo ""
echo "=========================================="
echo ""

systemctl status "$APP_NAME" --no-pager || true
