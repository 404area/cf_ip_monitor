"""
扫描策略层。

负责: 把一堆 CF /24 段，按"C段早停 + 时间窗口 + 已知沉默段过滤"产出探测任务。

核心流程 (每次全量调度时):
  1. 拉所有 /24, 过滤掉仍在沉默 TTL 内的段
  2. 对每个段生成"采样探测任务" (TCP_PING + HTTP_TRACE)
  3. Agent 回报后, 由 process_results 判断:
     - 全失败 -> 段置为 silent
     - 至少一个 ok -> 段置为 alive, 扩展为完整 /24 的 TCP 任务
  4. 完整探测完成后, 由 scoring 模块挑选候选 IP 加做 SPEED / TRACEROUTE 任务

所有入队的任务都带 scan_round_id + stage, 便于跨轮断点续传和进度统计。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from .ip_sources import (
    all_hosts_in_24,
    explode_to_24,
    fetch_cloudflare_cidrs,
    sample_offsets_in_24,
)
from .storage import Storage


logger = logging.getLogger(__name__)


@dataclass
class StrategyConfig:
    isps: List[str]
    c_segment_probe_offsets: List[int]
    c_segment_silent_ttl_hours: int
    speed_test_bytes: int
    speed_test_timeout_s: float
    trace_max_hops: int = 20
    trace_per_hop_timeout_s: float = 2.0


class Strategy:
    """无状态的策略对象, 所有持久化都走 Storage。"""

    def __init__(self, storage: Storage, cfg: StrategyConfig) -> None:
        self.storage = storage
        self.cfg = cfg

    # ---------------------------------------------------------------- planning
    def plan_initial_sampling(self, scan_round_id: str) -> int:
        """每个 ISP × 每个 /24 段产出 sample 任务。返回入队任务总数。

        scan_round_id 用于把任务和 c_segment_state.sampled_round 关联,
        process_results 阶段才能事件驱动地决定"本轮哪些段已采样完毕"。
        """
        cidrs_24 = explode_to_24(fetch_cloudflare_cidrs())
        logger.info("plan_initial_sampling: %d /24 segments (round=%s)",
                    len(cidrs_24), scan_round_id)

        total = 0
        for isp in self.cfg.isps:
            silent = set(self.storage.get_silent_segments(isp))
            rows: List[Tuple] = []
            for cidr in cidrs_24:
                if cidr in silent:
                    continue
                ips = sample_offsets_in_24(cidr, self.cfg.c_segment_probe_offsets)
                for ip in ips:
                    payload = json.dumps({
                        "ip": ip,
                        "kind": "tcp_ping",
                        "port": 443,
                        "cidr": cidr,
                        "stage": "sample",
                    })
                    rows.append((
                        uuid.uuid4().hex, ip, isp, "tcp_ping",
                        "sample", scan_round_id, payload,
                    ))
            n = self.storage.enqueue_tasks(rows)
            total += n
            logger.info("  isp=%s enqueued=%d (silent skipped=%d)", isp, n, len(silent))
        return total

    def expand_alive_segments(
        self,
        alive_segments: Iterable[Tuple[str, str]],
        scan_round_id: str,
    ) -> int:
        """对采样阶段判定为 alive 的 (isp, cidr24) 做全段 TCP_PING 任务。"""
        rows: List[Tuple] = []
        for isp, cidr in alive_segments:
            for ip in all_hosts_in_24(cidr):
                payload = json.dumps({
                    "ip": ip, "kind": "tcp_ping", "port": 443,
                    "cidr": cidr, "stage": "expand",
                })
                rows.append((
                    uuid.uuid4().hex, ip, isp, "tcp_ping",
                    "expand", scan_round_id, payload,
                ))
        return self.storage.enqueue_tasks(rows)

    def queue_http_trace(
        self,
        candidates: Iterable[Tuple[str, str]],
        scan_round_id: str,
    ) -> int:
        """对 TCP 通过的候选 IP 发起 cdn-cgi/trace 任务, 识别 colo。"""
        rows: List[Tuple] = []
        for isp, ip in candidates:
            payload = json.dumps({
                "ip": ip, "kind": "http_trace", "port": 443,
                "http_host": "speed.cloudflare.com",
            })
            rows.append((
                uuid.uuid4().hex, ip, isp, "http_trace",
                "http_trace", scan_round_id, payload,
            ))
        return self.storage.enqueue_tasks(rows)

    def queue_speed_test(
        self,
        candidates: Iterable[Tuple[str, str]],
        scan_round_id: str,
    ) -> int:
        """对延迟最优的候选下发下载测速任务。"""
        rows: List[Tuple] = []
        for isp, ip in candidates:
            payload = json.dumps({
                "ip": ip, "kind": "speed", "port": 443,
                "speed_bytes": self.cfg.speed_test_bytes,
                "speed_timeout_s": self.cfg.speed_test_timeout_s,
                "http_host": "speed.cloudflare.com",
            })
            rows.append((
                uuid.uuid4().hex, ip, isp, "speed",
                "speed", scan_round_id, payload,
            ))
        return self.storage.enqueue_tasks(rows)

    def queue_traceroute(
        self,
        candidates: Iterable[Tuple[str, str]],
        scan_round_id: str,
    ) -> int:
        """对优选 IP 下发 traceroute 任务 (低频, 单 IP 5-15s)。"""
        rows: List[Tuple] = []
        for isp, ip in candidates:
            payload = json.dumps({
                "ip": ip, "kind": "traceroute", "port": 443,
                "max_hops": self.cfg.trace_max_hops,
                "per_hop_timeout_s": self.cfg.trace_per_hop_timeout_s,
            })
            rows.append((
                uuid.uuid4().hex, ip, isp, "traceroute",
                "traceroute", scan_round_id, payload,
            ))
        return self.storage.enqueue_tasks(rows)

    # ---------------------------------------------------------------- consume
    def consume_sample_results(
        self,
        isp: str,
        results_by_cidr: dict,
        scan_round_id: str,
    ) -> List[Tuple[str, str]]:
        """根据采样阶段结果, 把沉默段标记入库, 返回 alive 段供后续展开。

        results_by_cidr: { cidr24: List[ok_bool] }
        """
        ttl_s = self.cfg.c_segment_silent_ttl_hours * 3600
        alive: List[Tuple[str, str]] = []
        for cidr, oks in results_by_cidr.items():
            total = len(oks)
            ok_cnt = sum(1 for x in oks if x)
            if ok_cnt == 0:
                self.storage.set_segment_state(
                    isp, cidr, "silent", ttl_s, 0, total,
                    sampled_round=scan_round_id,
                )
            else:
                self.storage.set_segment_state(
                    isp, cidr, "alive", ttl_s, ok_cnt, total,
                    sampled_round=scan_round_id,
                )
                alive.append((isp, cidr))
        return alive


def now_ms() -> int:
    return int(time.time() * 1000)
