"""
下载速度测试, 走 speed.cloudflare.com/__down?bytes=N。

返回 MB/s (而不是 Mb/s) 以匹配用户配置 (>=10 MB/s)。
"""
from __future__ import annotations

from typing import Optional, Tuple

from .http_client import http_stream_download


def speed_test(
    ip: str,
    host: str = "speed.cloudflare.com",
    bytes_to_download: int = 50_000_000,
    timeout: float = 15.0,
) -> Tuple[bool, Optional[float], Optional[int], Optional[float], Optional[str]]:
    """
    返回: (ok, mbps, downloaded_bytes, duration_s, error)
    其中 mbps 单位是 MB/s。
    """
    path = f"/__down?bytes={bytes_to_download}"
    downloaded, duration, err = http_stream_download(
        ip=ip,
        host=host,
        path=path,
        max_bytes=bytes_to_download,
        timeout=timeout,
    )
    if err and downloaded == 0:
        return False, None, 0, duration, err
    if downloaded < 1024 * 100:
        return False, None, downloaded, duration, "too_few_bytes"
    speed_mbps = (downloaded / duration) / (1024 * 1024)
    return True, round(speed_mbps, 2), downloaded, round(duration, 3), err
