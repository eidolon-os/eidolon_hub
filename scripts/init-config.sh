#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p config

if [ -f .env ] && [ ! -f config/.env ]; then
  cp .env config/.env
  echo "[INFO] migrated .env -> config/.env"
fi
if [ ! -f config/settings.yaml ]; then
  cp config/settings.example.yaml config/settings.yaml
  echo "[INFO] created config/settings.yaml"
fi
if [ ! -f config/.env ]; then
  cp config/.env.example config/.env
  echo "[INFO] created config/.env"
fi
echo "[INFO] done."
