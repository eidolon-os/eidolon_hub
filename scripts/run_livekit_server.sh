#!/usr/bin/env bash
#
# LiveKit Server — 本地启动脚本
#
# 用法:
#   ./run_livekit_server.sh       启动 (前台)
#   ./run_livekit_server.sh -d    启动 (后台守护)
#   ./run_livekit_server.sh stop  停止
#   ./run_livekit_server.sh restart  重启
#

set -euo pipefail

# ---------------------------------------------------------------
# 配置
# ---------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${LIVEKIT_CONFIG:-$SCRIPT_DIR/livekit.yaml}"
LOG_DIR="$SCRIPT_DIR/../logs"
PID_FILE="$LOG_DIR/livekit_server.pid"
LOG_FILE="$LOG_DIR/livekit_server.log"

# 自动查找二进制
LIVEKIT_BIN="${LIVEKIT_BIN:-}"
if [[ -z "$LIVEKIT_BIN" ]]; then
    for _path in \
        "/opt/homebrew/bin/livekit-server" \
        "/usr/local/bin/livekit-server" \
        "$(which livekit-server 2>/dev/null)"; do
        if [[ -x "${_path}" ]]; then
            LIVEKIT_BIN="$_path"
            break
        fi
    done
fi

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo ""; echo -e "${CYAN}==== $* ====${NC}"; }

# ---------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------
check_prereqs() {
    if [[ ! -x "$LIVEKIT_BIN" ]]; then
        error "找不到 livekit-server 二进制"
        error "请确认已安装: brew install livekit-server  (macOS)"
        error "或: curl -sL https://get.livekit.io | bash  (Linux)"
        exit 1
    fi

    if [[ ! -f "$CONFIG_FILE" ]]; then
        error "配置文件不存在: $CONFIG_FILE"
        exit 1
    fi

    mkdir -p "$LOG_DIR"
}

# ---------------------------------------------------------------
# 启动
# ---------------------------------------------------------------
do_start() {
    if is_running; then
        warn "LiveKit Server 已在运行 (PID $(cat "$PID_FILE"))，无需重复启动"
        exit 0
    fi

    section "启动 LiveKit Server (本地开发模式)"

    info "版本:     $(eval "$LIVEKIT_BIN --version")"
    info "配置:     $CONFIG_FILE"
    info "日志:     $LOG_FILE"
    echo ""

    # --config   加载 yaml，keys / bind_addresses / rtc 等均以配置文件为准
    # --dev      开发模式：debug 日志 + pprof，
    #            生产部署请去掉此 flag，改用 deploy/livekit/docker-compose.yaml

    local CMD=(
        "$LIVEKIT_BIN"
        --config "$CONFIG_FILE"
    )

    if [[ "${1:-}" == "-d" || "${1:-}" == "--daemon" ]]; then
        info "以后台守护模式启动..."
        nohup "${CMD[@]}" --dev > "$LOG_FILE" 2>&1 &
        local PID=$!
        echo "$PID" > "$PID_FILE"
        sleep 1
        if is_running; then
            info "已启动，PID=$PID"
            info "日志: tail -f $LOG_FILE"
        else
            error "启动失败，查看日志: cat $LOG_FILE"
            exit 1
        fi
    else
        info "启动中（前台模式，Ctrl+C 停止）..."
        echo ""
        "${CMD[@]}" --dev 2>&1
    fi
}

# ---------------------------------------------------------------
# 停止
# ---------------------------------------------------------------
do_stop() {
    if ! is_running; then
        warn "LiveKit Server 未运行"
        return 0
    fi

    local PID
    PID=$(cat "$PID_FILE")
    section "停止 LiveKit Server"
    info "发送 SIGTERM -> PID $PID"
    kill -TERM "$PID" 2>/dev/null || true

    # 等待进程退出（最多 5s）
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

# ---------------------------------------------------------------
# 重启
# ---------------------------------------------------------------
do_restart() {
    do_stop
    sleep 1
    do_start
}

# ---------------------------------------------------------------
# 状态
# ---------------------------------------------------------------
do_status() {
    if is_running; then
        info "LiveKit Server 运行中，PID=$(cat "$PID_FILE")"
    else
        info "LiveKit Server 未运行"
    fi
}

# ---------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------
is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# ---------------------------------------------------------------
# 入口
# ---------------------------------------------------------------
check_prereqs

case "${1:-}" in
    -d|--daemon)
        do_start -d
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_restart
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
        echo "  (无参数)    前台启动，可直接查看日志输出"
        echo "  -d/--daemon 后台守护模式启动"
        echo "  stop        停止"
        echo "  restart     重启"
        echo "  status      查看运行状态"
        exit 1
        ;;
esac
