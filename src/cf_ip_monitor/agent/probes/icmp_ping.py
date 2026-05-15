"""
ICMP ping 补充探测 (A4)。

- 调用系统 `ping` 命令, 单文件 0 依赖。
- TCP_PING 用来过门 (CF 边缘连通性 + TLS 端口);
  ICMP 用来反映 "纯网络层 RTT", 不受 CF 边缘负载影响。
- 不强制要求 root: 系统 `ping` 在 Linux/macOS 上是 setuid 程序, 用户态也能跑。
- 缺 `ping` 命令时直接返回 ok=False, 不影响主流程。

返回结构同 PingStats, 复用同一个 dataclass。
"""
from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from typing import List, Optional

from .tcp_ping import PingStats, _percentile


logger = logging.getLogger(__name__)


_RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


def _build_cmd(ip: str, count: int, timeout_s: float) -> Optional[List[str]]:
    if not shutil.which("ping"):
        return None
    sys = platform.system()
    if sys == "Darwin":
        # macOS: -c count, -W ms (per-probe wait), -i interval (default 1s, 用 0.2 加速)
        return [
            "ping", "-n", "-c", str(count),
            "-i", "0.2",
            "-W", str(int(max(100, timeout_s * 1000))),
            ip,
        ]
    # Linux: -c count, -W seconds, -i seconds
    return [
        "ping", "-n", "-c", str(count),
        "-i", "0.2",
        "-W", str(int(max(1, timeout_s))),
        ip,
    ]


def icmp_ping(
    ip: str,
    count: int = 6,
    timeout_s: float = 2.0,
    warmup: int = 1,
) -> PingStats:
    """对单个 IP 跑系统 ping count 次, 解析 RTT 并返回 PingStats。

    实现层面 warmup 通过 "结果列表丢弃前 warmup 个" 实现, 因为系统 ping 没法
    单独区分 "热身" 和 "正式", 但 ICMP 是无连接的, 冷启动影响远小于 TCP。
    """
    total = max(1, count + warmup)
    cmd = _build_cmd(ip, total, timeout_s)
    if cmd is None:
        return PingStats(ok=False, samples=0, error="ping_not_installed")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=total * (timeout_s + 0.5) + 3.0,
        )
    except subprocess.TimeoutExpired:
        return PingStats(ok=False, samples=0, error="subprocess_timeout")
    except Exception as e:
        return PingStats(ok=False, samples=0, error=f"subprocess_error:{e}")

    rtts: List[float] = []
    for line in (proc.stdout or "").splitlines():
        m = _RTT_RE.search(line)
        if m:
            try:
                rtts.append(float(m.group(1)))
            except ValueError:
                pass

    # 丢弃 warmup
    rtts = rtts[warmup:]
    succ = len(rtts)
    loss_rate = round((count - succ) / max(1, count), 3) if count > 0 else 1.0

    if succ == 0:
        return PingStats(ok=False, samples=0, loss_rate=loss_rate,
                         error="all_failed")
    rtts.sort()
    p50 = _percentile(rtts, 0.5)
    p95 = _percentile(rtts, 0.95)
    avg = sum(rtts) / succ
    return PingStats(
        ok=True, samples=succ,
        latency_min=round(rtts[0], 1),
        latency_p50=round(p50, 1),
        latency_p95=round(p95, 1),
        latency_avg=round(avg, 1),
        jitter_ms=round(max(0.0, p95 - p50), 1),
        loss_rate=loss_rate,
        error=None,
    )
