#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! command -v docker >/dev/null 2>&1; then
    echo "==> Installing Docker..."
    curl -fsSL https://get.docker.com | bash
fi

echo "==> Starting LiveKit Server..."
cd "$SCRIPT_DIR"
docker compose up -d

echo "==> Checking status..."
sleep 
docker compose ps

echo ""
echo "=========================================="
echo "  LiveKit Server Deployed"
echo "=========================================="
echo "  Signal (wss):   livekit.eidolon.yangtzeailab.com"
echo "  TURN (tls):     livekit-turn.eidolon.yangtzeailab.com:5349"
echo "  Logs:           docker compose logs -f"
echo "=========================================="
echo ""
echo "Firewall ports to open:"
echo "  443/tcp   - Nginx HTTPS + TURN/TLS"
echo "  5349/tcp  - TURN/TLS (via firewall)"
echo "  3478/udp  - TURN/UDP"
echo "  7881/tcp  - WebRTC TCP fallback"
echo "  50000-60000/udp - WebRTC media"
