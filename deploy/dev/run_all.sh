#!/usr/bin/env bash
# 一键启动 / 停止：LiveKit(-d) + Hub API + Web + Admin
#
#   ./run_all.sh        启动（全部后台，日志在仓库 logs/run_all_*.log）
#   ./run_all.sh stop   停止
#   ./run_all.sh status 查看是否认为仍在运行
#
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

LOG_DIR="$REPO_ROOT/logs"
PID_FILE="$LOG_DIR/run_all.pids"
mkdir -p "$LOG_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

any_run_all_alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local line pid
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  done < "$PID_FILE"
  return 1
}

clear_stale_pid_file() {
  [[ -f "$PID_FILE" ]] || return 0
  if any_run_all_alive; then
    return 1
  fi
  rm -f "$PID_FILE"
  return 0
}

do_start() {
  if [[ -f "$PID_FILE" ]] && any_run_all_alive; then
    error "run_all 已在运行（见 $PID_FILE）。先执行: $0 stop"
    exit 1
  fi
  clear_stale_pid_file || true

  info "启动 LiveKit（后台）..."
  RUN_ALL_INNER=1 "$SCRIPT_DIR/run-livekit.sh" -d

  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT

  info "启动 Hub API -> $LOG_DIR/run_all_api.log"
  nohup "$SCRIPT_DIR/run-api.sh" >> "$LOG_DIR/run_all_api.log" 2>&1 &
  echo "hub=$!" >> "$tmp"

  info "启动 Web (Next) -> $LOG_DIR/run_all_web.log"
  nohup "$SCRIPT_DIR/web/run-client.sh" >> "$LOG_DIR/run_all_web.log" 2>&1 &
  echo "web=$!" >> "$tmp"

  info "启动 Admin (Vite) -> $LOG_DIR/run_all_admin.log"
  nohup "$SCRIPT_DIR/web/run-admin.sh" >> "$LOG_DIR/run_all_admin.log" 2>&1 &
  echo "admin=$!" >> "$tmp"

  mv "$tmp" "$PID_FILE"
  trap - EXIT

  info "已全部后台启动。"
  info "  PID 列表: $PID_FILE"
  info "  停止: $0 stop"
  dev_print_access_urls
}

do_stop() {
  info "停止 LiveKit..."
  "$SCRIPT_DIR/run-livekit.sh" stop 2>/dev/null || true

  if [[ ! -f "$PID_FILE" ]]; then
    info "无 run_all PID 文件（Hub/Web/Admin 或未由此脚本启动）。"
    return 0
  fi

  info "停止 Hub / Web / Admin..."
  local line pid key
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      info "  SIGTERM $key (PID $pid)"
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done < "$PID_FILE"

  sleep 2

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      warn "  SIGKILL $key (PID $pid)"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done < "$PID_FILE"

  rm -f "$PID_FILE"
  info "已停止。"
}

do_status() {
  echo -e "${CYAN}==== run_all 状态 ====${NC}"
  if [[ -f "$PID_FILE" ]] && any_run_all_alive; then
    info "run_all 管理的进程仍在运行:"
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      echo "  $line"
    done < "$PID_FILE"
  else
    info "无活跃 run_all 进程（或 PID 文件已过期）。"
  fi
  echo ""
  "$SCRIPT_DIR/run-livekit.sh" status 2>/dev/null || true
}

case "${1:-start}" in
  start|"")
    do_start
    ;;
  stop)
    do_stop
    ;;
  status)
    do_status
    ;;
  *)
    echo "用法: $0 [start|stop|status]" >&2
    exit 1
    ;;
esac
