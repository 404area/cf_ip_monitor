#!/usr/bin/env bash
# 一键构建并启动 Docker 栈 (Master + 可选 Agent)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-master}"   # master | all

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

docker compose build

case "$MODE" in
  master)
    docker compose up -d master
    echo "Master 已启动: http://localhost:${MASTER_PORT:-8088}"
    ;;
  all)
    docker compose up -d master
    docker compose --profile agents up -d
    echo "Master + Agents 已启动"
    ;;
  *)
    echo "用法: $0 [master|all]"
    exit 1
    ;;
esac

docker compose ps
