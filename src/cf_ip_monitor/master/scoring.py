"""
优选评分。

输入: 近 N 天聚合的 (ip, isp, latency, speed, colo, enrichment, route_label) 样本
输出: 按 (ISP × Region) 分桶, 每桶 top_n 的 IP 列表

评分模型 (E2 引入路由因子后):

  完整模式 (speed_required=True):
      score = 0.5 * (1 - lat_p50/max_lat) * jitter_penalty
            + 0.3 * min(speed/min_speed, 3)/3
            + 0.2 * route_quality

  纯延迟模式 (speed_required=False):
      score = 0.7 * (1 - lat_p50/max_lat) * jitter_penalty
            + 0.3 * route_quality

  jitter_penalty = max(0.5, 1 - jitter_ms / max_jitter_ms)
  没有 traceroute 数据时 route_quality 用默认 0.4 (相当于"未知"线路)

  门槛 (与之前一致):
  - latency_p50 <= max_latency_ms
  - speed_required=True 时还要求 speed_p50 >= min_speed_mbps

入选优先级仍然是分桶: COLO -> 中文 region 名 (NRT->日本, HKG->香港 ...)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from .labeling import _QUALITY
from .storage import RouteLabel, SegmentAggregate, Storage


logger = logging.getLogger(__name__)


# Cloudflare colo 三字码到地区中文的映射 (常见亚太+美西)
# 完整列表见 https://en.wikipedia.org/wiki/List_of_Cloudflare_data_centers
COLO_TO_REGION_CN = {
    # 日本
    "NRT": "日本", "KIX": "日本", "ITM": "日本", "FUK": "日本",
    # 香港
    "HKG": "香港",
    # 新加坡
    "SIN": "新加坡",
    # 台湾
    "TPE": "台湾", "KHH": "台湾",
    # 韩国
    "ICN": "韩国",
    # 美西
    "LAX": "美西", "SJC": "美西", "SEA": "美西", "PDX": "美西",
    # 美东/中部
    "EWR": "美东", "IAD": "美东", "ATL": "美东", "DFW": "美中", "ORD": "美中",
    # 欧洲常见
    "FRA": "德国", "AMS": "荷兰", "LHR": "英国", "CDG": "法国",
    # 国内 (回源很少落到这里, 但留着)
    "PEK": "中国", "PVG": "中国", "CAN": "中国",
    # 其他亚太
    "BKK": "泰国", "KUL": "马来西亚", "MNL": "菲律宾", "SGN": "越南",
}


@dataclass
class BestPick:
    isp: str
    region: str
    ip: str
    latency_ms: float
    speed_mbps: float
    score: float
    colo: Optional[str]
    # 新增富化展示字段, 仅用于输出/快照, 不参与计算
    jitter_ms: Optional[float] = None
    line_type: Optional[str] = None
    exit_city: Optional[str] = None
    dst_asn: Optional[int] = None


@dataclass
class ScoringConfig:
    min_speed_mbps: float
    max_latency_ms: float
    top_n_per_bucket: int
    lookback_days: int
    same_hour_window: int
    # False 时退化为"无测速"评分: 不要求 speed 字段, 不卡 min_speed 门槛
    speed_required: bool = True
    # 抖动惩罚: jitter 越大越扣分; 这个值对应 jitter_penalty = 0 的极端
    max_jitter_ms: float = 200.0
    # 路由质量权重启用与否
    route_quality_enabled: bool = True


class Scorer:
    def __init__(self, storage: Storage, cfg: ScoringConfig) -> None:
        self.storage = storage
        self.cfg = cfg

    def pick_best(self, isp: str) -> List[BestPick]:
        lookback_ms = self.cfg.lookback_days * 86400 * 1000
        rows = self.storage.aggregate_recent(
            isp=isp,
            lookback_ms=lookback_ms,
            same_hour_window=self.cfg.same_hour_window,
        )
        labels = self.storage.get_route_labels(isp) if self.cfg.route_quality_enabled else {}

        buckets: Dict[str, List[BestPick]] = {}
        for r in rows:
            if r.latency_p50 is None:
                continue
            if r.latency_p50 > self.cfg.max_latency_ms:
                continue
            if self.cfg.speed_required:
                if r.speed_p50 is None or r.speed_p50 < self.cfg.min_speed_mbps:
                    continue
            region = COLO_TO_REGION_CN.get((r.colo or "").upper(), r.colo or "未知")
            label = labels.get(r.ip)
            score = self._score(r, label)
            buckets.setdefault(region, []).append(BestPick(
                isp=isp, region=region, ip=r.ip,
                latency_ms=r.latency_p50,
                speed_mbps=r.speed_p50 if r.speed_p50 is not None else 0.0,
                score=score, colo=r.colo,
                jitter_ms=r.jitter_ms,
                line_type=label.line_type if label else None,
                exit_city=label.exit_city if label else None,
                dst_asn=r.dst_asn,
            ))
        out: List[BestPick] = []
        for region, picks in buckets.items():
            picks.sort(key=lambda x: x.score, reverse=True)
            out.extend(picks[: self.cfg.top_n_per_bucket])
        return out

    def _jitter_penalty(self, jitter_ms: Optional[float]) -> float:
        if jitter_ms is None or self.cfg.max_jitter_ms <= 0:
            return 1.0
        v = 1.0 - jitter_ms / self.cfg.max_jitter_ms
        return max(0.5, min(1.0, v))

    def _route_quality(self, label: Optional[RouteLabel]) -> float:
        if not self.cfg.route_quality_enabled:
            return 0.0
        if label is None:
            return _QUALITY.get("未知", 0.40)
        return label.quality or _QUALITY.get(label.line_type or "未知", 0.40)

    def _score(self, r: SegmentAggregate, label: Optional[RouteLabel]) -> float:
        lat_norm = max(0.0, 1.0 - (r.latency_p50 or 0.0) / self.cfg.max_latency_ms)
        lat_norm *= self._jitter_penalty(r.jitter_ms)
        rq = self._route_quality(label)
        if self.cfg.speed_required and r.speed_p50 is not None:
            spd_norm = min(r.speed_p50 / self.cfg.min_speed_mbps, 3.0) / 3.0
            if self.cfg.route_quality_enabled:
                return 0.5 * lat_norm + 0.3 * spd_norm + 0.2 * rq
            return 0.6 * lat_norm + 0.4 * spd_norm
        # 无测速 (或纯延迟模式)
        if self.cfg.route_quality_enabled:
            return 0.7 * lat_norm + 0.3 * rq
        return lat_norm

    def latency_top_candidates(
        self,
        isp: str,
        top_percentile: float,
    ) -> List[str]:
        """按延迟 (p50) 取前 percentile 的 IP, 用于决定哪些 IP 值得做速度测试。"""
        lookback_ms = self.cfg.lookback_days * 86400 * 1000
        rows = self.storage.aggregate_recent(
            isp=isp,
            lookback_ms=lookback_ms,
            same_hour_window=self.cfg.same_hour_window,
        )
        rows = [r for r in rows
                if r.latency_p50 is not None and r.latency_p50 <= self.cfg.max_latency_ms]
        rows.sort(key=lambda r: r.latency_p50 or 9999)
        cut = max(1, int(len(rows) * top_percentile))
        return [r.ip for r in rows[:cut]]
