#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run as root (sudo)."
    exit 1
fi

echo "==> Installing Nginx..."
if ! command -v nginx >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y nginx
    elif command -v yum >/dev/null 2>&1; then
        yum install -y nginx
    else
        echo "ERROR: Cannot detect package manager."
        exit 1
    fi
fi

echo "==> Installing SSL certificate..."
"$SCRIPT_DIR/ssl/install.sh"

echo "==> Installing Nginx site configs..."
for conf in "$SCRIPT_DIR"/*.conf; do
    name="$(basename "$conf")"
    echo "  - $name"
    cp "$conf" /etc/nginx/conf.d/
done

echo "==> Testing Nginx config..."
nginx -t

echo "==> Reloading Nginx..."
systemctl enable --now nginx || service nginx start
systemctl reload nginx || nginx -s reload

echo ""
echo "=========================================="
echo "  Nginx Deployed"
echo "=========================================="
echo "  Sites:"
for conf in "$SCRIPT_DIR"/*.conf; do
    name="$(basename "$conf" .conf)"
    echo "    https://$name"
done
echo ""
echo "  Status: systemctl status nginx"
echo "=========================================="
