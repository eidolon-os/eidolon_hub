#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Eidolon Hub Deploy — 一键部署脚本
# 用法:
#   ./deploy.sh              部署主 Hub 服务
#   ./deploy.sh api           部署 API 服务
#   ./deploy.sh web           部署 Web 前端
#   ./deploy.sh livekit       部署 LiveKit 服务器
#   ./deploy.sh nginx         部署 Nginx 反向代理
#   ./deploy.sh all           部署所有服务
#   ./deploy.sh --help        显示帮助信息
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
while [[ "$PROJECT_ROOT" != "/" ]]; do
    if [[ -f "$PROJECT_ROOT/pyproject.toml" ]]; then
        break
    fi
    PROJECT_ROOT="$(dirname "$PROJECT_ROOT")"
done
if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
    echo "ERROR: 未找到 pyproject.toml，无法确定项目根目录"
    exit 1
fi

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# 帮助信息
show_help() {
    cat << EOF
Eidolon Hub 部署脚本

用法:
    $0 [选项] [模块]

模块:
    (无)         部署主 Hub 服务
    api          部署 API 服务 (FastAPI)
    web          部署 Web 前端
    livekit      部署 LiveKit 服务器
    nginx        部署 Nginx 反向代理
    all          部署所有服务

选项:
    -h, --help   显示帮助信息

示例:
    $0           # 部署主服务
    $0 api       # 部署 API 服务
    $0 all       # 部署所有服务

EOF
}

# 部署 API 服务
deploy_api() {
    log_info "部署 API 服务..."
    "$SCRIPT_DIR/api/install.sh"
}

# 部署 Web 前端
deploy_web() {
    log_info "部署 Web 前端..."
    "$SCRIPT_DIR/client.sh"
}

# 部署 LiveKit
deploy_livekit() {
    log_info "部署 LiveKit 服务器..."
    "$SCRIPT_DIR/livekit/install.sh"
}

# 部署 Nginx
deploy_nginx() {
    log_info "部署 Nginx..."
    "$SCRIPT_DIR/nginx/install.sh"
}

# 部署主服务
deploy_main() {
    APP_NAME="eidolon-hub"
    APP_USER="${APP_USER:-eidolon}"
    APP_GROUP="${APP_GROUP:-eidolon}"
    SERVICE_NAME="${SERVICE_NAME:-eidolon-hub}"

    log_info "部署主 Hub 服务..."
    echo "==> [deploy] Project root: $PROJECT_ROOT"

    # 前置检查
    command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

    if [[ "$(id -u)" -ne 0 ]]; then
        echo "ERROR: Run as root (sudo) for systemd service installation."
        exit 1
    fi

    # 创建 venv + 安装依赖
    echo "==> [deploy] Creating virtual environment with uv..."
    if [ ! -d "$PROJECT_ROOT/.venv" ]; then
        uv venv "$PROJECT_ROOT/.venv" --python 3.12
    fi
    uv pip install -e "$PROJECT_ROOT" --python "$PROJECT_ROOT/.venv/bin/python"

    chown -R "$APP_USER:$APP_GROUP" "$PROJECT_ROOT/.venv"

    # 创建运行时目录
    echo "==> [deploy] Ensuring runtime directories..."
    mkdir -p "$PROJECT_ROOT/data"
    chown -R "$APP_USER:$APP_GROUP" "$PROJECT_ROOT/data"

    # 创建系统用户
    echo "==> [deploy] Setting up user: $APP_USER..."
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --user-group "$APP_USER" || true
    fi

    # 写入 systemd service 文件
    echo "==> [deploy] Writing systemd service: $SERVICE_NAME..."
    VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

    cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE
[Unit]
Description=Eidolon Hub — 统一设备接入层和 Agent 适配层
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$PROJECT_ROOT
ExecStart=$VENV_PYTHON -m hub.main
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"
Environment="EIDOLON_HUB_CONFIG=$PROJECT_ROOT/config/default.yaml"

ProtectSystem=full
ProtectHome=yes
ReadWritePaths=$PROJECT_ROOT/data $PROJECT_ROOT/.venv
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
SERVICE

    # 重载 systemd 并启动
    echo "==> [deploy] Reloading systemd..."
    systemctl daemon-reload

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "==> [deploy] Restarting $SERVICE_NAME..."
        systemctl restart "$SERVICE_NAME"
    else
        echo "==> [deploy] Enabling and starting $SERVICE_NAME..."
        systemctl enable --now "$SERVICE_NAME"
    fi

    echo "==> [deploy] Done. Check status: systemctl status $SERVICE_NAME"
    echo "==> [deploy] Logs: journalctl -u $SERVICE_NAME -f"
}

# 解析参数
TARGET="${1:-}"

case "$TARGET" in
    api)
        deploy_api
        ;;
    web)
        deploy_web
        ;;
    livekit)
        deploy_livekit
        ;;
    nginx)
        deploy_nginx
        ;;
    all)
        log_info "部署所有服务..."
        echo ""
        deploy_api
        echo ""
        deploy_web
        echo ""
        deploy_livekit
        echo ""
        deploy_nginx
        echo ""
        log_info "所有服务部署完成!"
        ;;
    --help|-h)
        show_help
        ;;
    "")
        deploy_main
        ;;
    *)
        log_warn "未知模块: $TARGET"
        show_help
        exit 1
        ;;
esac
