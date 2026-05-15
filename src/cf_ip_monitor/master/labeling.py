"""
路径标签生成 (E1/E2)。

基于 traceroute hops + ASN 序列, 自动推断:
  - line_type: 该 IP 在该 ISP 上走的线路类型 (163/CN2-GT/CN2-GIA/4837/9929/9808/CMI/普通/未知)
  - exit_city: 国内最后一跳所在城市 (广州/上海/北京/...)
  - cn_hops / overseas_hops: 跨境跳数计数
  - quality: 0~1 的路径质量分, scoring 时作为加权因子

线路 ASN 速查 (中国三大运营商常见骨干):

| ASN   | 名称                            | 线路类型说明                                 |
| 4134  | Chinanet / CN-163              | 电信普通 (163 骨干)                          |
| 4809  | China Telecom Next Generation  | 电信 CN2: 后端落在 4809 即 CN2-GT 或 CN2-GIA |
| 23764 | China Telecom CN2              | CN2-GIA 出口段                               |
| 58453 | China Mobile International     | CMI / 移动国际                               |
| 9808  | China Mobile                   | 移动普通                                     |
| 4837  | China Unicom 169 Backbone      | 联通普通 (CU169)                             |
| 9929  | China Unicom Premium (A2)      | 联通 9929 优质线                             |
| 10099 | China Unicom Global            | 联通国际                                     |
| 13335 | Cloudflare                     | 目的边缘                                     |

判定优先级 (高 -> 低):
  1. 路径包含 23764 -> CN2-GIA
  2. 路径包含 4809 但不含 23764 -> CN2-GT
  3. 路径包含 9929 -> CU-9929 (联通 A2)
  4. 路径包含 58453 -> CMI
  5. 路径包含 4837 且 ISP 是联通 -> CU-4837
  6. 路径包含 4134 且 ISP 是电信 -> CT-163
  7. 路径包含 9808 且 ISP 是移动 -> CM-9808
  8. 上面都不匹配 -> '未知'

出口城市判定:
  扫描 hops, 找到最后一个 country='CN' 的 hop, 取其 city。
  qqwry 命名对常见骨干节点已较准 (广州/上海/北京/深圳/福州/南京等)。
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, List, Optional, Tuple

from .enrich import Enricher, IPInfo
from .storage import RouteLabel, Storage


logger = logging.getLogger(__name__)


# ASN -> 线路标签的优先级序列 (大优先)
_LINE_RULES: List[Tuple[set, str]] = [
    ({23764},        "CN2-GIA"),
    ({4809},         "CN2-GT"),
    ({9929},         "CU-9929"),
    ({58453},        "CMI"),
    ({10099},        "CU-Global"),
]

# 落到 ISP 维度的 fallback (没匹配 premium 时按主干 ASN 判)
_LINE_BY_ISP_BACKBONE = {
    "电信": {4134: "CT-163"},
    "联通": {4837: "CU-4837"},
    "移动": {9808: "CM-9808"},
}


# 线路质量评分 (0~1, scoring 时作为路由因子)
_QUALITY = {
    "CN2-GIA": 1.00,
    "CU-9929": 0.95,
    "CN2-GT":  0.85,
    "CU-Global": 0.85,
    "CMI":     0.80,
    "CT-163":  0.55,
    "CU-4837": 0.55,
    "CM-9808": 0.55,
    "未知":     0.40,
}


# ip2region 的 ISP 字段值 (中文), 用作交叉验证
_ISP_CN_KEYWORDS = {
    "电信": "电信",
    "联通": "联通",
    "移动": "移动",
    "教育网": "教育网",
    "铁通": "铁通",
}


def _detect_line_type(
    asn_seq: List[Optional[int]],
    isp_cn_seq: List[Optional[str]],
    isp: str,
) -> str:
    """根据 ASN 序列 + ip2region.ISP 文本序列交叉判定线路类型。

    判定逻辑:
      1. premium ASN 命中直接返回 (CN2-GIA / CN2-GT / CU-9929 / CMI / CU-Global)
      2. fallback 主干: ASN 命中 + (可选) ISP_CN 一致 -> 加 confirmed 标签
         比如 ASN 4837 + ISP_CN 含 "联通" -> "CU-4837" (强信号)
         若 ASN 命中但 ISP_CN 全是其他运营商 -> 仍按 ASN 走 (可能 ISP_CN 没数据)
    """
    asns = {a for a in asn_seq if a is not None}
    isp_text = " ".join(x or "" for x in isp_cn_seq)

    # premium 线路 (ASN 优先, 这些 ASN 不和 ISP_CN 冲突时直接定)
    for rule_asns, label in _LINE_RULES:
        if asns & rule_asns:
            return label

    fallback = _LINE_BY_ISP_BACKBONE.get(isp, {})
    for asn, label in fallback.items():
        if asn in asns:
            # 交叉验证: 该 ISP 关键字出现在 isp_cn 文本里加分; 反向运营商出现降级
            expected_kw = {"CT": "电信", "CU": "联通", "CM": "移动"}.get(
                label.split("-", 1)[0], ""
            )
            if expected_kw and expected_kw in isp_text:
                return label
            # 没数据也直接信 ASN
            return label
    return "未知"


def _detect_exit_city(hops: List[dict]) -> Optional[str]:
    last_cn_city: Optional[str] = None
    for h in hops:
        if (h.get("country") or "") == "CN":
            c = h.get("city") or h.get("region")
            if c:
                last_cn_city = c
    return last_cn_city


def label_one(hops: List[dict], isp: str, ip: str) -> RouteLabel:
    """根据已经富化好的 hop 列表生成单个 IP 的 RouteLabel。"""
    asn_seq: List[Optional[int]] = [h.get("asn") for h in hops]
    isp_cn_seq: List[Optional[str]] = [h.get("isp_cn") for h in hops]
    line_type = _detect_line_type(asn_seq, isp_cn_seq, isp)
    quality = _QUALITY.get(line_type, 0.40)
    exit_city = _detect_exit_city(hops)
    cn_hops = sum(1 for h in hops if (h.get("country") or "") == "CN")
    overseas_hops = sum(
        1 for h in hops
        if h.get("country") and h.get("country") != "CN"
    )
    asn_path = ",".join(str(a) if a is not None else "" for a in asn_seq)
    return RouteLabel(
        ip=ip, isp=isp,
        measured_at=int(time.time() * 1000),
        line_type=line_type,
        exit_city=exit_city,
        cn_hops=cn_hops,
        overseas_hops=overseas_hops,
        asn_path=asn_path,
        quality=quality,
    )


def relabel_recent(storage: Storage, isps: Iterable[str], within_hours: int = 30) -> int:
    """对最近 within_hours 内有 traceroute 数据的 (ip, isp), 重新生成路由标签。

    返回更新的标签条数。
    """
    cutoff = int(time.time() * 1000) - within_hours * 3600 * 1000
    enricher = Enricher.get()  # noqa: F841 (这里只是确保库已加载)
    n = 0
    for isp in isps:
        ips = storage.recent_trace_ips(isp, within_ms=within_hours * 3600 * 1000)
        for ip in ips:
            hops = storage.fetch_trace_hops(ip, isp, since_ms=cutoff)
            if not hops:
                continue
            label = label_one(hops, isp, ip)
            storage.upsert_route_label(label)
            n += 1
    return n
