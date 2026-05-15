"""
IP 离线富化 (多源融合)。

数据源 (按字段查询优先级):
  ASN     : GeoLite2-ASN  ->  ip2region.ISP (text fallback)
  Country : dbip-city  ->  GeoLite2-Country  ->  ip2region  ->  qqwry
  Region  : [CN]  ip2region  ->  qqwry  ->  dbip-city  ->  MaxMind
            [非CN] dbip-city  ->  MaxMind  ->  ip2region
  City    : 同 Region 链
  ISP_CN  : ip2region.ISP  ->  qqwry 文本里挖 (电信/联通/移动/教育网/...)

为什么搞这么复杂?
- MaxMind 免费版在 CF 段空洞高达 70%, 单源不能用
- dbip-city 在国际段命中率 100% (实测), 是国际段主力
- ip2region 在国内段提供"结构化的 省/市/ISP"五字段, 比 qqwry 文本好用
- qqwry 保留作为 ISP 中文兜底, 数据已不更新但国内骨干段够用
- 多源 fallback 让任何一个字段都几乎不再为空

线程安全:
- maxminddb reader、qqwry 都是只读 mmap, 线程安全
- XdbSearcher 用 contentBuff 全内存模式, 跨线程并发 search 是安全的 (库说明)
- Enricher 是模块级单例

字段返回值约定:
- '0' / '' / None 都视为缺失
- ip2region 用 '0' 表示该字段缺失, 我们统一转 None
"""
from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

import geoip2.database
import geoip2.errors
import maxminddb
from qqwry import QQwry
from XdbSearchIP.xdbSearcher import XdbSearcher

from ..ipdata import (
    ASN_MMDB, CITY_MMDB, COUNTRY_MMDB, DBIP_MMDB, IP2REGION, QQWRY_DAT,
)


logger = logging.getLogger(__name__)


@dataclass
class IPInfo:
    """单个 IP 的富化结果。任何字段都可能为 None。"""

    asn: Optional[int] = None
    as_name: Optional[str] = None
    country: Optional[str] = None       # ISO code (CN/JP/US...)
    region: Optional[str] = None        # 省份 / 一级行政区
    city: Optional[str] = None
    isp_cn: Optional[str] = None        # "电信" / "联通" / "移动" / "教育网" / "Cloudflare" 等
    qqwry_raw: Optional[str] = None     # qqwry 原始描述, 调试用


# 私网段判定 (traceroute 第一跳常是内网网关, 没必要查库)
_PRIVATE_NETS = [
    ipaddress.ip_network(c) for c in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
        "224.0.0.0/4", "240.0.0.0/4",
    )
]

# 从 qqwry 描述文本里挖 ISP 关键字, 用于 ip2region 缺 ISP 时兜底
_QQWRY_ISP_KEYWORDS = [
    ("电信", "电信"), ("联通", "联通"), ("移动", "移动"),
    ("教育网", "教育网"), ("教育", "教育网"),
    ("铁通", "铁通"), ("广电", "广电"),
    ("CloudFlare", "Cloudflare"), ("Cloudflare", "Cloudflare"),
]

# ip2region 返回 '0' 表示该字段缺失
def _norm(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "0":
        return None
    return s


def _is_private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved:
        return True
    return any(a in n for n in _PRIVATE_NETS)


def _qqwry_extract_isp(text: str) -> Optional[str]:
    if not text:
        return None
    for kw, label in _QQWRY_ISP_KEYWORDS:
        if kw in text:
            return label
    return None


class Enricher:
    """整合 MaxMind / dbip / ip2region / qqwry 的富化器, 进程内单例。"""

    _instance_lock = threading.Lock()
    _instance: Optional["Enricher"] = None

    def __init__(self) -> None:
        self._asn = self._open_geoip2(ASN_MMDB, "ASN")
        self._city = self._open_geoip2(CITY_MMDB, "City")
        self._country = self._open_geoip2(COUNTRY_MMDB, "Country")
        self._dbip = self._open_maxminddb(DBIP_MMDB, "dbip-city")
        self._ip2region = self._open_ip2region()
        self._qqwry = self._open_qqwry()

    @staticmethod
    def _open_geoip2(path, name):
        try:
            r = geoip2.database.Reader(path)
            logger.info("enrich: opened %s (%s)", name, path)
            return r
        except Exception as e:
            logger.warning("enrich: open %s failed: %s", name, e)
            return None

    @staticmethod
    def _open_maxminddb(path, name):
        try:
            r = maxminddb.open_database(path)
            logger.info("enrich: opened %s (%s)", name, path)
            return r
        except Exception as e:
            logger.warning("enrich: open %s failed: %s", name, e)
            return None

    @staticmethod
    def _open_ip2region():
        try:
            buf = XdbSearcher.loadContentFromFile(dbfile=IP2REGION)
            s = XdbSearcher(contentBuff=buf)
            logger.info("enrich: opened ip2region (%s)", IP2REGION)
            return s
        except Exception as e:
            logger.warning("enrich: open ip2region failed: %s", e)
            return None

    @staticmethod
    def _open_qqwry():
        try:
            q = QQwry()
            if q.load_file(QQWRY_DAT):
                return q
            logger.warning("enrich: qqwry.load_file returned False")
        except Exception as e:
            logger.warning("enrich: open qqwry failed: %s", e)
        return None

    @classmethod
    def get(cls) -> "Enricher":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = Enricher()
            return cls._instance

    # ---------------------------------------------------------------- raw probes
    def _q_asn(self, ip: str):
        if self._asn is None:
            return None, None
        try:
            r = self._asn.asn(ip)
            return r.autonomous_system_number, r.autonomous_system_organization
        except geoip2.errors.AddressNotFoundError:
            return None, None
        except Exception as e:
            logger.debug("asn lookup %s err: %s", ip, e)
            return None, None

    def _q_dbip(self, ip: str) -> Optional[dict]:
        if self._dbip is None:
            return None
        try:
            return self._dbip.get(ip)
        except Exception as e:
            logger.debug("dbip lookup %s err: %s", ip, e)
            return None

    def _q_mm_city(self, ip: str):
        if self._city is None:
            return None
        try:
            return self._city.city(ip)
        except (geoip2.errors.AddressNotFoundError, Exception):
            return None

    def _q_mm_country(self, ip: str) -> Optional[str]:
        if self._country is None:
            return None
        try:
            r = self._country.country(ip)
            return r.country.iso_code
        except Exception:
            return None

    def _q_ip2region(self, ip: str) -> Optional[dict]:
        """ip2region 返回 'Country|Region|City|ISP|CountryCode' 五字段。"""
        if self._ip2region is None:
            return None
        try:
            raw = self._ip2region.searchByIPStr(ip)
        except Exception:
            return None
        if not raw:
            return None
        parts = raw.split("|")
        # 标准是 5 段, 但旧 xdb 可能 4 段; 兼容处理
        while len(parts) < 5:
            parts.append("0")
        country = _norm(parts[0])
        region  = _norm(parts[1])
        city    = _norm(parts[2])
        isp     = _norm(parts[3])
        ccode   = _norm(parts[4])
        return {
            "country_name": country, "region": region, "city": city,
            "isp": isp, "country_code": ccode, "raw": raw,
        }

    def _q_qqwry(self, ip: str):
        if self._qqwry is None:
            return None
        try:
            return self._qqwry.lookup(ip)
        except Exception:
            return None

    # ---------------------------------------------------------------- main lookup
    def lookup(self, ip: Optional[str]) -> IPInfo:
        if not ip or _is_private(ip):
            return IPInfo()

        info = IPInfo()

        # === 1) ASN (MaxMind 主源) ===
        asn, asn_name = self._q_asn(ip)
        info.asn = asn
        info.as_name = asn_name

        # === 2) 国家先取 dbip ===
        dbip_res = self._q_dbip(ip)
        if dbip_res:
            info.country = dbip_res.get("country_code") or None

        # === 3) 再用 ip2region 补全 / 双签 ===
        ip2 = self._q_ip2region(ip)

        # === 4) MaxMind country 兜底 ===
        if info.country is None:
            info.country = self._q_mm_country(ip)
        if info.country is None and ip2:
            info.country = ip2.get("country_code")

        # === 5) qqwry ===
        qres = self._q_qqwry(ip)
        if qres:
            info.qqwry_raw = " ".join(x for x in qres if x)
            first = qres[0] or ""
            if info.country is None and first.startswith("中国"):
                info.country = "CN"

        # === 6) Region / City 分支 ===
        if info.country == "CN":
            # 国内: ip2region 五字段最准
            if ip2:
                info.region = info.region or ip2.get("region")
                info.city = info.city or ip2.get("city")
                if ip2.get("isp"):
                    info.isp_cn = ip2["isp"]
            # qqwry 补
            if (info.region is None or info.city is None) and qres:
                desc = qres[0] or ""
                # 形如 "中国–北京–北京" 或 "中国 江苏 南京"
                m = re.match(r"^中国[–\s\-]+(?P<prov>[^\s–\-]+)(?:[–\s\-]+(?P<city>[^\s–\-]+))?", desc)
                if m:
                    info.region = info.region or m.group("prov")
                    info.city = info.city or m.group("city")
            # dbip / MaxMind 再兜
            if info.city is None and dbip_res:
                info.city = dbip_res.get("city") or None
                info.region = info.region or dbip_res.get("state1") or None
            if (info.region is None or info.city is None):
                mm = self._q_mm_city(ip)
                if mm:
                    info.region = info.region or (mm.subdivisions.most_specific.name or None)
                    info.city = info.city or (mm.city.name or None)
            # ISP_CN 兜底: qqwry 文本里挖
            if info.isp_cn is None and info.qqwry_raw:
                info.isp_cn = _qqwry_extract_isp(info.qqwry_raw)
        else:
            # 非国内: dbip 优先, MaxMind 次之, ip2region 兜底
            if dbip_res:
                info.region = dbip_res.get("state1") or None
                info.city = dbip_res.get("city") or None
            if (info.region is None or info.city is None):
                mm = self._q_mm_city(ip)
                if mm:
                    info.region = info.region or (mm.subdivisions.most_specific.name or None)
                    info.city = info.city or (mm.city.name or None)
            if (info.region is None or info.city is None) and ip2:
                info.region = info.region or ip2.get("region")
                info.city = info.city or ip2.get("city")
            # 国外 IP 也可能有 ISP 字段 (e.g. ip2region 给 "Cloudflare LLC")
            if ip2 and ip2.get("isp"):
                info.isp_cn = ip2["isp"]

        return info
