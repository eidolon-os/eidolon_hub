#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Eidolon Hub Deploy — 一键部署脚本
# 用法: ./deploy.sh [--systemd] [--restart]
# ============================================================

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="eidolon-hub"
APP_USER="${APP_USER:-eidolon}"
APP_GROUP="${APP_GROUP:-eidolon}"
SERVICE_NAME="${SERVICE_NAME:-eidolon-hub}"

echo "==> [deploy] Project root: $PROJECT_ROOT"

# --------------------------------------------------
# 0. 前置检查
# --------------------------------------------------
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run as root (sudo) for systemd service installation."
    exit 1
fi

# --------------------------------------------------
# 选定目录中创建 venv + 安装依赖
# --------------------------------------------------
echo "==> [deploy] Creating virtual environment with uv..."
if [ ! -d "$PROJECT_ROOT/.venv" ]; then
    uv venv "$PROJECT_ROOT/.venv" --python 3.11
fi
uv pip install -e "$PROJECT_ROOT" --python "$PROJECT_ROOT/.venv/bin/python"

# 安装完毕后修正权限
chown -R "$APP_USER:$APP_GROUP" "$PROJECT_ROOT/.venv"

# 确保运行时数据和配置文件目录存在
echo "==> [deploy] Ensuring runtime directories..."
mkdir -p "$PROJECT_ROOT/data"
chown -R "$APP_USER:$APP_GROUP" "$PROJECT_ROOT/data"

# --------------------------------------------------
# 2. 创建系统用户（如果不存在）
# --------------------------------------------------
echo "==> [deploy] Setting up user: $APP_USER..."
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --user-group "$APP_USER" || true
fi

# --------------------------------------------------
# 3. 写入 systemd service 文件
# --------------------------------------------------
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

# --------------------------------------------------
# 4. 重载 systemd 并启动
# --------------------------------------------------
echo "==> [deploy] Reloading systemd..."
systemctl daemon-reload

if [[ "${1:-}" == "--restart" ]] || systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "==> [deploy] Restarting $SERVICE_NAME..."
    systemctl restart "$SERVICE_NAME"
else
    echo "==> [deploy] Enabling and starting $SERVICE_NAME..."
    systemctl enable --now "$SERVICE_NAME"
fi

echo "==> [deploy] Done. Check status: systemctl status $SERVICE_NAME"
echo "==> [deploy] Logs: journalctl -u $SERVICE_NAME -f"
