#!/usr/bin/env bash
# 一键构建并启动 Docker 栈
#
# 用法:
#   ./scripts/docker-deploy.sh <mode> [args...]
#
# 模式:
#   master            仅 Master
#   master+ct         Master + 电信 Agent (同机)
#   master+cu         Master + 联通 Agent (同机)
#   master+cm         Master + 移动 Agent (同机)
#   all               Master + 三网 Agent (电信 / 联通 / 移动)
#
#   edge              在已运行的 Master 前面起一个 nginx 反代 (profile=edge)
#                     前置: 复制 deploy/nginx/master.conf.example → master.conf 改域名
#                          放证书到 deploy/nginx/certs/{fullchain,privkey}.pem
#   master-edge       Master + nginx 反代 (一次起两个)
#   master-edge+ct    Master + 电信 Agent + nginx 反代 (一次起三个)
#
#   agent             单独跑一个 Agent (Master 在别处, 用 docker-compose.agent.yml)
#                     必须先设置环境变量:
#                       AGENT_MASTER_URL=https://master.example.com (或 http://1.2.3.4:8088)
#                       AGENT_AUTH_TOKEN=<与 master 一致>
#                       AGENT_ISP=电信
#                       AGENT_NODE_NAME=ct-hk-01
#
#   agent-ct          只起 docker-compose.yml 里的 agent-ct (适合 master 已跑过, 单加一个)
#   agent-cu          同上, 联通
#   agent-cm          同上, 移动
#
#   down              停掉本机所有 Master + Agent + nginx
#   status            docker compose ps -a
#   logs [service]    跟随日志, 不指定则全部
#
# 示例:
#   ./scripts/docker-deploy.sh master
#   ./scripts/docker-deploy.sh master+ct
#   ./scripts/docker-deploy.sh all
#   AGENT_MASTER_URL=http://1.2.3.4:8088 AGENT_ISP=电信 AGENT_NODE_NAME=ct-vps-01 \
#       ./scripts/docker-deploy.sh agent

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-}"
shift || true

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-1}"
}

ensure_nginx_conf() {
  if [[ ! -f deploy/nginx/master.conf ]]; then
    echo "未找到 deploy/nginx/master.conf, 从模板复制..."
    cp deploy/nginx/master.conf.example deploy/nginx/master.conf
    echo "请编辑 deploy/nginx/master.conf 修改 server_name 为你的域名"
  fi
  if [[ ! -f deploy/nginx/certs/fullchain.pem ]] || [[ ! -f deploy/nginx/certs/privkey.pem ]]; then
    echo "警告: 未找到 TLS 证书 deploy/nginx/certs/fullchain.pem & privkey.pem"
    echo "    生产环境用 Let's Encrypt; 测试用 ./scripts/gen-self-signed-cert.sh <domain>"
    echo "    nginx 启动会失败, 先放好证书再 up"
  fi
}

ensure_files() {
  if [[ ! -f config.yaml ]]; then
    echo "未找到 config.yaml, 从示例复制..."
    cp config.example.yaml config.yaml
    echo "请编辑 config.yaml (至少修改 master.auth_token 与 agent.auth_token)"
  fi

  if [[ ! -f .env ]]; then
    cp deploy/docker/.env.example .env
    echo "已创建 .env, 可按需修改端口与 Agent 节点名"
  fi

  if [[ ! -f uv.lock ]]; then
    echo "生成 uv.lock ..."
    command -v uv >/dev/null || { echo "请先安装 uv: https://docs.astral.sh/uv/"; exit 1; }
    uv lock
  fi

  if [[ ! -f src/cf_ip_monitor/ipdata/GeoLite2-ASN.mmdb ]]; then
    echo "IP 离线库缺失, 自动下载 (GitHub 镜像, 无需 MaxMind 注册)..."
    chmod +x scripts/download_ipdata.sh
    ./scripts/download_ipdata.sh || {
      echo "IP 库下载失败, 请检查网络后重试: ./scripts/download_ipdata.sh"
      exit 1
    }
  fi
}

ensure_remote_agent_env() {
  : "${AGENT_MASTER_URL:?需要设置 AGENT_MASTER_URL=https://your-master:8088}"
  : "${AGENT_ISP:?需要设置 AGENT_ISP (电信 / 联通 / 移动)}"
  : "${AGENT_NODE_NAME:?需要设置 AGENT_NODE_NAME, 例如 ct-hk-01}"
}

build_main()  { docker compose build "$@"; }
build_agent_only() { docker compose -f docker-compose.agent.yml build "$@"; }

case "$MODE" in
  master)
    ensure_files
    build_main
    docker compose up -d master
    echo "Master 已启动: http://localhost:${MASTER_PORT:-8088}"
    ;;

  master+ct|master+cu|master+cm)
    ensure_files
    build_main
    docker compose up -d master
    case "$MODE" in
      master+ct) svc=agent-ct ;;
      master+cu) svc=agent-cu ;;
      master+cm) svc=agent-cm ;;
    esac
    docker compose --profile agents up -d "$svc"
    echo "Master + $svc 已启动"
    ;;

  all)
    ensure_files
    build_main
    docker compose up -d master
    docker compose --profile agents up -d
    echo "Master + 三网 Agent 已启动"
    ;;

  edge)
    ensure_files
    ensure_nginx_conf
    build_main
    docker compose --profile edge up -d nginx
    echo "nginx 反代已启动: http://localhost:${NGINX_HTTP_PORT:-80} https://localhost:${NGINX_HTTPS_PORT:-443}"
    ;;

  master-edge)
    ensure_files
    ensure_nginx_conf
    build_main
    docker compose up -d master
    docker compose --profile edge up -d nginx
    echo "Master + nginx 反代已启动"
    ;;

  master-edge+ct|master-edge+cu|master-edge+cm)
    ensure_files
    ensure_nginx_conf
    build_main
    docker compose up -d master
    case "$MODE" in
      master-edge+ct) svc=agent-ct ;;
      master-edge+cu) svc=agent-cu ;;
      master-edge+cm) svc=agent-cm ;;
    esac
    docker compose --profile agents up -d "$svc"
    docker compose --profile edge up -d nginx
    echo "Master + $svc + nginx 反代已启动"
    ;;

  agent-ct|agent-cu|agent-cm)
    ensure_files
    build_main
    docker compose --profile agents up -d "$MODE"
    echo "$MODE 已启动 (Master 需要已经在同一 compose 网络中运行)"
    ;;

  agent)
    ensure_remote_agent_env
    ensure_files
    build_agent_only
    docker compose -f docker-compose.agent.yml up -d
    echo "远程 Agent 已启动, 指向 Master: $AGENT_MASTER_URL"
    docker compose -f docker-compose.agent.yml ps
    exit 0
    ;;

  down)
    docker compose --profile agents --profile edge down || true
    docker compose -f docker-compose.agent.yml down || true
    exit 0
    ;;

  status)
    echo "--- docker-compose.yml ---"
    docker compose ps -a || true
    echo "--- docker-compose.agent.yml ---"
    docker compose -f docker-compose.agent.yml ps -a || true
    exit 0
    ;;

  logs)
    svc="${1:-}"
    if [[ -n "$svc" ]]; then
      docker compose logs -f --tail=200 "$svc"
    else
      docker compose logs -f --tail=200
    fi
    exit 0
    ;;

  ""|-h|--help|help)
    usage 0
    ;;

  *)
    echo "未知模式: $MODE" >&2
    usage 1
    ;;
esac

docker compose ps
