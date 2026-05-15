"""
TCP 443 端口连通性 + RTT 探测。

设计要点 (A1/A2):

1. 首次 TCP 握手会受 ARP 解析 / 国内运营商 QoS 冷启动 / CF anycast 路由刷新影响,
   实测往往明显偏高甚至超时, 直接把它纳入 avg 会污染统计。
   做法: 单独跑 N 次 "warmup", 结果不计入样本, 只用来"热"链路状态。
2. 正式样本至少 5 次, 用来稳定计算 min/p50/p95/jitter。
3. 失败 (连不上 / 超时) 算丢包, 不算样本; 但失败次数会拉高 loss_rate。

返回结构 (PingStats) 字段含义:

| 字段        | 单位 | 含义                                                |
| latency_min | ms   | 最低成功 RTT (排除 warmup)                          |
| latency_p50 | ms   | 50 分位 RTT, 评分主用此值                           |
| latency_p95 | ms   | 95 分位 RTT, 反映"最差情况"                         |
| latency_avg | ms   | 兼容旧 latency_ms 字段, 同样排除 warmup             |
| jitter_ms   | ms   | p95 - p50, 越大说明链路抖动越大, scoring 用来降权    |
| loss_rate   | 0~1  | (warmup+sample) 失败次数 / 总尝试数                 |
| samples     | int  | 实际计入统计的成功样本数 (排除 warmup)              |
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PingStats:
    ok: bool
    samples: int
    latency_min: Optional[float] = None
    latency_p50: Optional[float] = None
    latency_p95: Optional[float] = None
    latency_avg: Optional[float] = None
    jitter_ms: Optional[float] = None
    loss_rate: Optional[float] = None
    error: Optional[str] = None


def tcp_ping_once(ip: str, port: int, timeout: float) -> Optional[float]:
    try:
        start = time.perf_counter()
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None


def _percentile(sorted_values: List[float], q: float) -> float:
    """简单分位数 (不插值, 取最近的样本)。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = int(round(q * (len(sorted_values) - 1)))
    idx = max(0, min(idx, len(sorted_values) - 1))
    return sorted_values[idx]


def tcp_ping(
    ip: str,
    port: int = 443,
    timeout: float = 2.0,
    retry: int = 6,
    warmup: int = 1,
) -> PingStats:
    """对单个 (ip, port) 执行 (warmup + retry) 次 TCP 握手, 返回 PingStats。

    - warmup 次结果不计入样本 (首次握手偏高)
    - 总尝试数 = warmup + retry, 用于丢包率统计
    - 全部失败时 ok=False, 仍返回 error="all_failed"
    """
    if retry < 1:
        retry = 1
    if warmup < 0:
        warmup = 0

    total = warmup + retry
    samples: List[float] = []
    success_in_warmup = 0

    for i in range(total):
        v = tcp_ping_once(ip, port, timeout)
        if i < warmup:
            if v is not None:
                success_in_warmup += 1
            continue
        if v is not None:
            samples.append(v)

    succ_total = success_in_warmup + len(samples)
    loss_rate = round((total - succ_total) / total, 3) if total > 0 else 1.0

    if not samples:
        return PingStats(
            ok=False, samples=0,
            loss_rate=loss_rate,
            error="all_failed",
        )

    samples.sort()
    p50 = _percentile(samples, 0.5)
    p95 = _percentile(samples, 0.95)
    avg = sum(samples) / len(samples)
    return PingStats(
        ok=True,
        samples=len(samples),
        latency_min=round(samples[0], 1),
        latency_p50=round(p50, 1),
        latency_p95=round(p95, 1),
        latency_avg=round(avg, 1),
        jitter_ms=round(max(0.0, p95 - p50), 1),
        loss_rate=loss_rate,
        error=None,
    )
