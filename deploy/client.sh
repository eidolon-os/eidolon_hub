#!/usr/bin/env bash
set -euo pipefail

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
WEB_DIR="$PROJECT_ROOT/client/web"
SERVICE_NAME="eidolon-web"
APP_USER="${APP_USER:-eidolon}"
APP_GROUP="${APP_GROUP:-eidolon}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run as root (sudo)."
    exit 1
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --user-group "$APP_USER" || true
fi

echo "==> Building Next.js frontend..."
cd "$WEB_DIR"
npm install
npm run build

echo "==> Writing systemd service: $SERVICE_NAME..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE
[Unit]
Description=Eidolon Web Client (Next.js)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$WEB_DIR
ExecStart=$(which node) $WEB_DIR/node_modules/.bin/next start --port 3000
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
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
SERVICE

echo "==> Reloading systemd..."
systemctl daemon-reload

if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
else
    systemctl enable --now "$SERVICE_NAME"
fi

echo ""
echo "=========================================="
echo "  Web Client Deployed"
echo "=========================================="
echo "  URL:   https://webclient.eidolon.yangtzeailab.com"
echo "  Status: systemctl status $SERVICE_NAME"
echo "  Logs:   journalctl -u $SERVICE_NAME -f"
echo "=========================================="
