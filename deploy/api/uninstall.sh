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
APP_NAME="eidolon-hub-api"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo ""
echo "=========================================="
echo "  Eidolon Hub API 卸载脚本"
echo "=========================================="
echo ""

if [[ "$(id -u)" -ne 0 ]]; then
    log_error "请使用 root 用户运行 (sudo)"
    exit 1
fi

if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    log_info "停止 systemd 服务..."
    systemctl stop "$APP_NAME"
    systemctl disable "$APP_NAME"
    rm -f "/etc/systemd/system/${APP_NAME}.service"
    systemctl daemon-reload
    log_info "systemd 服务已卸载"
else
    log_info "未找到运行中的 API 服务"
fi

echo ""
echo "=========================================="
echo "  卸载完成"
echo "=========================================="
echo ""
echo "  如需完全清理，可手动执行:"
echo "    rm -rf $PROJECT_ROOT/.venv"
echo "    userdel eidolon  # 如不再需要"
echo ""
echo "=========================================="
