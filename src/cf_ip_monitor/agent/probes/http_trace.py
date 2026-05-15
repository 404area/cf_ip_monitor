"""
Cloudflare colo 识别。

curl --resolve speed.cloudflare.com:443:<IP> https://speed.cloudflare.com/cdn-cgi/trace
返回形如:
    fl=64f150
    h=speed.cloudflare.com
    ip=1.2.3.4
    ts=...
    colo=NRT
    loc=JP
    ...
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from .http_client import http_get


def http_trace(
    ip: str,
    host: str = "speed.cloudflare.com",
    timeout: float = 5.0,
) -> Tuple[bool, Optional[float], Optional[str], Optional[str]]:
    """
    返回: (ok, latency_ms, colo, error)
    """
    start = time.perf_counter()
    try:
        status, body = http_get(ip, host, "/cdn-cgi/trace", timeout=timeout)
    except Exception as e:
        return False, None, None, f"err:{e}"
    rtt = (time.perf_counter() - start) * 1000.0

    if status != 200:
        return False, rtt, None, f"status:{status}"

    text = body.decode("utf-8", errors="ignore")
    colo: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("colo="):
            colo = line.split("=", 1)[1].strip().upper()
            break
    if not colo:
        return False, rtt, None, "no_colo_in_body"
    return True, round(rtt, 1), colo, None
