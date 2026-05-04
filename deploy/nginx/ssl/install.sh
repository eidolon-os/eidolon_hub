#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run as root (sudo)."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NGINX_SSL_DIR="/etc/nginx/ssl/yangtzeailab.com"

echo "==> Creating $NGINX_SSL_DIR ..."
mkdir -p "$NGINX_SSL_DIR"

echo "==> Installing certificate files..."
cp "$SCRIPT_DIR/yangtzeailab.com.pem" "$NGINX_SSL_DIR/fullchain.pem"
cp "$SCRIPT_DIR/yangtzeailab.com.key" "$NGINX_SSL_DIR/privkey.pem"
chmod 640 "$NGINX_SSL_DIR"/fullchain.pem "$NGINX_SSL_DIR"/privkey.pem

echo "==> Done. Files installed:"
ls -la "$NGINX_SSL_DIR"
