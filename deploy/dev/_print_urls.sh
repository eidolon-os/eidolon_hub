# 由 deploy/dev 下各启动脚本 source；需已定义 SCRIPT_DIR 为 deploy/dev 目录
# 可选环境变量: HUB_PORT（默认 8081）、DEV_URL_HOST（默认 127.0.0.1）

dev_print_access_urls() {
  local hub_port="${HUB_PORT:-8081}"
  local host="${DEV_URL_HOST:-127.0.0.1}"
  echo ""
  echo "======== 访问地址（本机 ${host}；局域网请换成本机 IP）========"
  echo "  LiveKit 信令     ws://${host}:7880"
  echo "  Hub API          http://${host}:${hub_port}/"
  echo "  Hub API 文档     http://${host}:${hub_port}/docs"
  echo "  Web 客户端       http://${host}:3000"
  echo "  Admin            http://${host}:5174"
  echo "================================================================"
  echo ""
}
