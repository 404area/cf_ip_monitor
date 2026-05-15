"""
Cloudflare IP 段来源。

主源: https://www.cloudflare.com/ips-v4 (官方文档地址)
拉取失败时使用内置兜底列表，保证调度永不阻塞。
"""
from __future__ import annotations

import ipaddress
import logging
from typing import List

import httpx


logger = logging.getLogger(__name__)


# 截至 2026 年初的兜底列表，定期 fetch_cloudflare_cidrs() 会覆盖它
FALLBACK_CF_CIDRS_V4 = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
]


def fetch_cloudflare_cidrs(timeout: float = 8.0) -> List[str]:
    """从 Cloudflare 官方拉取最新 v4 段列表，失败时返回兜底。"""
    try:
        r = httpx.get("https://www.cloudflare.com/ips-v4", timeout=timeout)
        r.raise_for_status()
        cidrs = [line.strip() for line in r.text.splitlines() if line.strip()]
        if cidrs:
            return cidrs
    except Exception as e:
        logger.warning("fetch cloudflare cidrs failed: %s, fallback to builtin", e)
    return FALLBACK_CF_CIDRS_V4


def explode_to_24(cidrs: List[str]) -> List[str]:
    """把任意 v4 CIDR 拆成 /24 单位，便于按 C 段做早停策略。"""
    out: List[str] = []
    for c in cidrs:
        net = ipaddress.ip_network(c, strict=False)
        if net.prefixlen >= 24:
            out.append(str(net))
            continue
        for sub in net.subnets(new_prefix=24):
            out.append(str(sub))
    return out


def sample_offsets_in_24(cidr24: str, offsets: List[int]) -> List[str]:
    """从一个 /24 内取指定尾号的 IP。"""
    base = ipaddress.ip_network(cidr24, strict=False).network_address
    ips: List[str] = []
    for off in offsets:
        if 0 < off < 255:
            ips.append(str(base + off))
    return ips


def all_hosts_in_24(cidr24: str) -> List[str]:
    """返回 /24 内的全部主机 IP (排除网络/广播)。"""
    return [str(h) for h in ipaddress.ip_network(cidr24, strict=False).hosts()]
