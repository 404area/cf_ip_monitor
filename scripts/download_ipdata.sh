#!/usr/bin/env bash
# 下载 CF IP Monitor 所需的 IP 离线库 (全部可自动下载, 无需 MaxMind 注册)
#
# GeoLite2 来源: GitHub 社区镜像 (P3TERX / FyraLabs), 供无法访问 MaxMind 官网的地区使用。
# 数据遵循 GeoLite2 EULA, 仅供个人/非商用; 商用请自行评估合规性。
#
# 用法:
#   ./scripts/download_ipdata.sh          # 缺啥下啥
#   FORCE=1 ./scripts/download_ipdata.sh  # 强制全部重新下载
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/src/cf_ip_monitor/ipdata"
FORCE="${FORCE:-0}"

mkdir -p "$DEST"
cd "$DEST"

echo "==> 目标目录: $DEST"
echo

download() {
  local url="$1" out="$2"
  if [[ "$FORCE" != "1" && -f "$out" && -s "$out" ]]; then
    echo "  [skip] $out 已存在 ($(du -h "$out" | cut -f1))"
    return
  fi
  echo "  [get]  $out"
  echo "         $url"
  if ! curl -fL --retry 3 --connect-timeout 60 --max-time 600 -o "$out" "$url"; then
    echo "  [fail] $out" >&2
    rm -f "$out"
    return 1
  fi
}

download_mirror() {
  local out="$1"; shift
  if [[ "$FORCE" != "1" && -f "$out" && -s "$out" ]]; then
    echo "  [skip] $out 已存在 ($(du -h "$out" | cut -f1))"
    return 0
  fi
  for url in "$@"; do
    echo "  [try]  $out <- $url"
    if curl -fL --retry 2 --connect-timeout 60 --max-time 600 -o "$out" "$url"; then
      echo "  [ok]   $out ($(du -h "$out" | cut -f1))"
      return 0
    fi
    rm -f "$out"
  done
  echo "  [fail] 所有镜像均失败: $out" >&2
  return 1
}

echo ">>> GeoLite2 (GitHub 镜像, 无需 MaxMind 注册)"
download_mirror "GeoLite2-ASN.mmdb" \
  "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-ASN.mmdb" \
  "https://github.com/FyraLabs/geolite2/releases/latest/download/GeoLite2-ASN.mmdb" \
  "https://cdn.jsdelivr.net/gh/Loyalsoldier/geoip@release/GeoLite2-ASN.mmdb"

download_mirror "GeoLite2-City.mmdb" \
  "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-City.mmdb" \
  "https://github.com/FyraLabs/geolite2/releases/latest/download/GeoLite2-City.mmdb" \
  "https://cdn.jsdelivr.net/gh/Loyalsoldier/geoip@release/GeoLite2-City.mmdb"

download_mirror "GeoLite2-Country.mmdb" \
  "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-Country.mmdb" \
  "https://github.com/FyraLabs/geolite2/releases/latest/download/GeoLite2-Country.mmdb" \
  "https://cdn.jsdelivr.net/gh/Loyalsoldier/geoip@release/GeoLite2-Country.mmdb"

echo
echo ">>> dbip-city-ipv4 (国际段城市, 推荐)"
download "https://github.com/sapics/ip-location-db/raw/main/dbip-city-mmdb/dbip-city-ipv4.mmdb" \
         "dbip-city-ipv4.mmdb"

echo ">>> ip2region (国内段 ISP/省市, 推荐)"
download "https://github.com/lionsoul2014/ip2region/raw/master/data/ip2region.xdb" \
         "ip2region_v4.xdb"

echo ">>> qqwry (国内 ISP 兜底, 可选)"
download "https://github.com/metowolf/qqwry.dat/releases/latest/download/qqwry.dat" \
         "qqwry.dat"

echo
missing=()
for f in GeoLite2-ASN.mmdb dbip-city-ipv4.mmdb ip2region_v4.xdb; do
  [[ -f "$f" && -s "$f" ]] || missing+=("$f")
done

echo "==> 当前状态:"
for f in GeoLite2-ASN.mmdb GeoLite2-City.mmdb GeoLite2-Country.mmdb \
         dbip-city-ipv4.mmdb ip2region_v4.xdb qqwry.dat; do
  if [[ -f "$f" && -s "$f" ]]; then
    printf "  ✓ %-24s %s\n" "$f" "$(du -h "$f" | cut -f1)"
  else
    printf "  ✗ %-24s 缺失\n" "$f"
  fi
done

if ((${#missing[@]})); then
  echo
  echo "错误: 必须文件缺失: ${missing[*]}"
  exit 1
fi

echo
echo "完成。重启 master: docker compose restart master"
