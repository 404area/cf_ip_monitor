#!/usr/bin/env bash
# 生成自签 TLS 证书 (仅测试用)
#
# 用法:
#   ./scripts/gen-self-signed-cert.sh master.example.com
#
# 输出:
#   deploy/nginx/certs/fullchain.pem
#   deploy/nginx/certs/privkey.pem
#
# 生产环境请用 Let's Encrypt:
#   sudo certbot certonly --webroot -w /var/www/certbot -d master.example.com
#   把 /etc/letsencrypt/live/<domain>/fullchain.pem 和 privkey.pem 软链到 deploy/nginx/certs/

set -euo pipefail

DOMAIN="${1:-master.example.com}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/deploy/nginx/certs"

mkdir -p "$OUT"

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$OUT/privkey.pem" \
    -out    "$OUT/fullchain.pem" \
    -days 365 \
    -subj "/CN=$DOMAIN" \
    -addext "subjectAltName=DNS:$DOMAIN"

chmod 600 "$OUT/privkey.pem"

echo "已生成自签证书 (有效期 365 天, CN=$DOMAIN):"
echo "  $OUT/fullchain.pem"
echo "  $OUT/privkey.pem"
echo ""
echo "提示: agent 端 httpx 默认会校验证书, 自签会失败。"
echo "若用自签证书测试, 请在 agent 容器内设置环境变量 SSL_CERT_FILE 指向 fullchain.pem,"
echo "或者改为 http (不加密) 反代。"
