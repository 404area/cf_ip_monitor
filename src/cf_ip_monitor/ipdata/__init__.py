"""IP 离线数据库目录 (多源融合)。

存放:
  GeoLite2-ASN.mmdb     MaxMind ASN, RIR 数据, 用作 ASN 主源
  GeoLite2-City.mmdb    MaxMind 城市, 覆盖率较低, 作兜底
  GeoLite2-Country.mmdb MaxMind 国家, 仅 country code, 兜底
  dbip-city-ipv4.mmdb   DB-IP 城市, 实测覆盖 CF anycast IP 100%, 国际段主力
                        注意: schema 与 MaxMind 不同, 必须用 maxminddb 直读, 不能用 geoip2 reader
  ip2region_v4.xdb      ip2region v4, 字段化 国家|区域|省|市|ISP, 国内段+ISP 主力
  qqwry.dat             纯真 IP 库, 国内段历史描述兜底, 仍可用作 ISP 中文兜底

下载地址:
  GeoLite2-*           https://www.maxmind.com/en/geolite2/signup  (需注册免费账号)
  dbip-city-ipv4       https://github.com/sapics/ip-location-db/tree/main/dbip-city-mmdb
  ip2region_v4.xdb     https://github.com/lionsoul2014/ip2region/raw/master/data/ip2region.xdb
  qqwry.dat            https://github.com/metowolf/qqwry.dat/releases

模块只对外暴露各文件的绝对路径常量, reader 在 master.enrich 模块构造。
"""
from __future__ import annotations

import os


# 运行时可通过环境变量 CF_IPDATA_DIR 覆盖, 适配 Docker / wheel 安装场景
# (wheel 装到 site-packages 后, 包内置目录是只读且与项目源码解耦的, 不便外挂)
_ENV_DIR = os.environ.get("CF_IPDATA_DIR", "").strip()
_BASE = _ENV_DIR or os.path.dirname(os.path.abspath(__file__))

ASN_MMDB     = os.path.join(_BASE, "GeoLite2-ASN.mmdb")
CITY_MMDB    = os.path.join(_BASE, "GeoLite2-City.mmdb")
COUNTRY_MMDB = os.path.join(_BASE, "GeoLite2-Country.mmdb")
DBIP_MMDB    = os.path.join(_BASE, "dbip-city-ipv4.mmdb")
IP2REGION    = os.path.join(_BASE, "ip2region_v4.xdb")
QQWRY_DAT    = os.path.join(_BASE, "qqwry.dat")


def available() -> dict:
    """便于运行期诊断: 返回每个库的存在/大小情况。"""
    out = {}
    for k, p in (
        ("asn",      ASN_MMDB),
        ("city",     CITY_MMDB),
        ("country",  COUNTRY_MMDB),
        ("dbip",     DBIP_MMDB),
        ("ip2region", IP2REGION),
        ("qqwry",    QQWRY_DAT),
    ):
        out[k] = {
            "path": p,
            "exists": os.path.exists(p),
            "size": os.path.getsize(p) if os.path.exists(p) else 0,
        }
    return out
