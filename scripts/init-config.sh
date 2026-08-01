#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p config

if [ ! -f config/.env ]; then
  cp config/.env.example config/.env
  echo "[INFO] created config/.env"
fi
echo "[INFO] done."
