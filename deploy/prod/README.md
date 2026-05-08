# 生产编排（Docker Compose）

在 **本目录** 准备 `.env`（从 `.env.example` 复制并修改）。

```bash
cp .env.example .env
# 编辑 .env：至少设置 EIDOLON_LIVEKIT_URL、NEXT_PUBLIC_*、VITE_ADMIN_API_BASE 为实际可达地址
docker compose up -d --build
```

## 服务与端口

| 服务    | 容器名           | 宿主机端口 |
|---------|------------------|------------|
| LiveKit | eidolon-livekit  | host 7880 等 |
| Hub API | eidolon-hub-api  | 8081       |
| Web     | eidolon-web      | 3000       |
| Admin   | eidolon-admin    | 5174       |

LiveKit 使用 **`network_mode: host`**，UDP 媒体端口由 `livekit.yaml` 中 `rtc.port_range_*` 与 `turn` 决定；云主机需在安全组放行 **7880/tcp**、**3478/udp**、**50000–60000/udp**（及文档所需端口）。

## Hub 访问 LiveKit

Hub 在 **bridge 网络** 的容器内运行，通过 **`http://host.docker.internal:7880`** 访问宿主机上的 LiveKit。Linux 需 Docker 20.10+；`docker-compose.yaml` 已配置 `extra_hosts: host.docker.internal:host-gateway`。

## 构建期变量（Web / Admin）

`NEXT_PUBLIC_*` 与 `VITE_ADMIN_API_BASE` 在 **构建镜像时** 写入静态资源。修改后需 **`docker compose build --no-cache web admin`**（或整体重建）再启动。

浏览器访问页面时，这些 URL 必须是 **客户端能解析的地址**（局域网 IP 或域名），不要填写仅在容器内有效的 hostname（除非用户浏览器也能解析）。

## 数据持久化

Hub 将 `data/`、`logs/` 挂载为命名卷 `hub_data`、`hub_logs`。

## 常用命令

```bash
docker compose logs -f hub
docker compose down
```
