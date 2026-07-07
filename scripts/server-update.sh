#!/usr/bin/env bash
# 服务器上更新并部署 (master + 联通 agent)
# 用法: cd /home/cfyouxuan && ./scripts/server-update.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> git pull"
git pull --ff-only

echo "==> IP 离线库"
chmod +x scripts/download_ipdata.sh
./scripts/download_ipdata.sh

echo "==> Docker 构建 & 启动 (master + agent-cu)"
chmod +x scripts/docker-deploy.sh
./scripts/docker-deploy.sh master+cu

echo
echo "==> 健康检查"
sleep 5
curl -sf "http://127.0.0.1:${MASTER_PORT:-8088}/healthz" && echo " Master OK"
docker compose ps
