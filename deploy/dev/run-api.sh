#!/usr/bin/env bash
# 在仓库根目录启动 Hub API（热重载）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_print_urls.sh"

repo_root() {
  local d="$SCRIPT_DIR"
  while [[ "$d" != "/" ]]; do
    if [[ -f "$d/pyproject.toml" ]]; then
      echo "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

REPO_ROOT="$(repo_root)" || {
  echo "[ERROR] 无法找到项目根目录（缺少 pyproject.toml）" >&2
  exit 1
}

cd "$REPO_ROOT"

export HUB_HOST="${HUB_HOST:-0.0.0.0}"
export HUB_PORT="${HUB_PORT:-8081}"

dev_print_access_urls

if command -v uv >/dev/null 2>&1; then
  exec uv run uvicorn hub.main:app --host "$HUB_HOST" --port "$HUB_PORT" --reload "$@"
elif [[ -x "$REPO_ROOT/.venv/bin/uvicorn" ]]; then
  exec "$REPO_ROOT/.venv/bin/uvicorn" hub.main:app --host "$HUB_HOST" --port "$HUB_PORT" --reload "$@"
else
  echo "[ERROR] 需要 uv 或 .venv/bin/uvicorn。请先: uv sync" >&2
  exit 1
fi
