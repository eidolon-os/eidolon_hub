#!/usr/bin/env bash
#
# NATS Server（JetStream）— deploy/dev 启动脚本
#
# 用法:
#   ./run-nats.sh       启动 (前台)
#   ./run-nats.sh -d    启动 (后台守护)
#   ./run-nats.sh stop  停止
#   ./run-nats.sh restart  重启
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

LOG_DIR="${NATS_LOG_DIR:-$REPO_ROOT/logs}"
PID_FILE="$LOG_DIR/nats_server.pid"
LOG_FILE="$LOG_DIR/nats_server.log"
STORE_DIR="${NATS_STORE_DIR:-$REPO_ROOT/data/nats-jetstream}"

NATS_CLIENT_PORT="${NATS_CLIENT_PORT:-4222}"
NATS_HTTP_PORT="${NATS_HTTP_PORT:-8222}"

NATS_BIN="${NATS_BIN:-}"
if [[ -z "$NATS_BIN" ]]; then
  for _path in \
    "/opt/homebrew/bin/nats-server" \
    "/usr/local/bin/nats-server" \
    "$(command -v nats-server 2>/dev/null)"; do
    if [[ -n "${_path}" && -x "${_path}" ]]; then
      NATS_BIN="$_path"
      break
    fi
  done
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo ""; echo -e "${CYAN}==== $* ====${NC}"; }

check_prereqs() {
  if [[ ! -x "$NATS_BIN" ]]; then
    error "找不到 nats-server 二进制"
    error "请确认已安装: brew install nats-server  (macOS)"
    error "或: https://docs.nats.io/running-a-nats-service/introduction/installation"
    exit 1
  fi

  mkdir -p "$LOG_DIR" "$STORE_DIR"
}

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

do_start() {
  if is_running; then
    warn "NATS Server 已在运行 (PID $(cat "$PID_FILE"))，无需重复启动"
    if [[ "${RUN_ALL_INNER:-}" != 1 ]]; then
      dev_print_access_urls
    fi
    exit 0
  fi

  section "启动 NATS Server (deploy/dev, JetStream)"

  info "版本:     $($NATS_BIN -v 2>&1 | head -1)"
  info "客户端:   nats://127.0.0.1:${NATS_CLIENT_PORT}"
  info "监控 HTTP: http://127.0.0.1:${NATS_HTTP_PORT}"
  info "JetStream 存储: $STORE_DIR"
  info "日志:     $LOG_FILE"
  echo ""

  local CMD=(
    "$NATS_BIN"
    -js
    -sd "$STORE_DIR"
    -p "$NATS_CLIENT_PORT"
    -m "$NATS_HTTP_PORT"
  )

  if [[ "${1:-}" == "-d" || "${1:-}" == "--daemon" ]]; then
    info "以后台守护模式启动..."
    nohup "${CMD[@]}" > "$LOG_FILE" 2>&1 &
    local PID=$!
    echo "$PID" > "$PID_FILE"
    sleep 1
    if is_running; then
      info "已启动，PID=$PID"
      info "日志: tail -f $LOG_FILE"
      if [[ "${RUN_ALL_INNER:-}" != 1 ]]; then
        dev_print_access_urls
      fi
    else
      error "启动失败，查看日志: cat $LOG_FILE"
      exit 1
    fi
  else
    info "启动中（前台模式，Ctrl+C 停止）..."
    dev_print_access_urls
    echo ""
    exec "${CMD[@]}" 2>&1
  fi
}

do_stop() {
  if ! is_running; then
    warn "NATS Server 未运行"
    return 0
  fi

  local PID
  PID=$(cat "$PID_FILE")
  section "停止 NATS Server"
  info "发送 SIGTERM -> PID $PID"
  kill -TERM "$PID" 2>/dev/null || true

  local i=0
  while is_running && ((i < 10)); do
    sleep 0.5
    ((i++))
  done

  if is_running; then
    warn "进程未响应 SIGTERM，发送 SIGKILL"
    kill -KILL "$PID" 2>/dev/null || true
    sleep 0.5
  fi

  rm -f "$PID_FILE"
  info "已停止"
}

do_restart() {
  do_stop
  sleep 1
  do_start "${1:-}"
}

do_status() {
  if is_running; then
    info "NATS Server 运行中，PID=$(cat "$PID_FILE")"
    info "  nats://127.0.0.1:${NATS_CLIENT_PORT}  监控 http://127.0.0.1:${NATS_HTTP_PORT}"
  else
    info "NATS Server 未运行"
  fi
}

check_prereqs

case "${1:-}" in
  -d|--daemon)
    do_start -d
    ;;
  stop)
    do_stop
    ;;
  restart)
    do_restart "${2:-}"
    ;;
  status)
    do_status
    ;;
  "")
    do_start
    ;;
  *)
    echo "用法: $0 [start|-d|--daemon|stop|restart|status]"
    echo ""
    echo "  (无参数)    前台启动"
    echo "  -d/--daemon 后台守护模式启动（启用 JetStream）"
    echo "  stop        停止"
    echo "  restart     重启"
    echo "  status      查看运行状态"
    exit 1
    ;;
esac
