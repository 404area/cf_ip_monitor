"""
华为云 DNS 智能解析推送。

文档:
- https://support.huaweicloud.com/api-dns/dns_api_64003.html (record set)
- 智能解析需要在 record set 上指定 line 字段, 内置线路如 Dianxin/Liantong/Yidong

策略:
- 同一 (zone, record_name, type=A, line) 视作一组, 用列表 IP 全量替换
- 先尝试 list 找出已存在的 record set, 若有则 update, 否则 create
- 只处理 A 记录

注意: 这里使用 HTTP + AK/SK 自签名 (避免引入华为云 SDK 体积), 兼容公开 v2.1 接口。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

from ..scoring import BestPick


logger = logging.getLogger(__name__)


HOST_TPL = "dns.{region}.myhuaweicloud.com"
ALGORITHM = "SDK-HMAC-SHA256"


def _resolve_env(value: str) -> str:
    """支持 ${ENV_NAME} 写法。"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


class HuaweiCloudDNSExporter:
    def __init__(
        self,
        ak: str,
        sk: str,
        region: str,
        zone_id: str,
        record_name: str,
        ttl: int,
        isp_line_map: Dict[str, str],
        records_per_line: int = 5,
    ) -> None:
        self.ak = _resolve_env(ak)
        self.sk = _resolve_env(sk)
        self.region = _resolve_env(region)
        self.zone_id = _resolve_env(zone_id)
        self.record_name = _resolve_env(record_name)
        self.ttl = ttl
        self.isp_line_map = isp_line_map
        self.records_per_line = records_per_line
        self.host = HOST_TPL.format(region=self.region)
        self.endpoint = f"https://{self.host}"
        if not self.ak or not self.sk:
            logger.warning("HuaweiCloud DNS exporter created without AK/SK, calls will fail")

    # ------------------------------------------------------------------ public
    def export(self, picks_by_isp: Dict[str, List[BestPick]]) -> None:
        for isp, picks in picks_by_isp.items():
            line = self.isp_line_map.get(isp)
            if not line:
                logger.warning("no line mapping for isp=%s, skip", isp)
                continue
            ips = [p.ip for p in picks[: self.records_per_line]]
            if not ips:
                logger.info("isp=%s no picks, skip", isp)
                continue
            self._upsert_recordset(line, ips)

    def _upsert_recordset(self, line: str, ips: List[str]) -> None:
        existing = self._find_recordset(line)
        body = {
            "name": self.record_name,
            "type": "A",
            "ttl": self.ttl,
            "records": ips,
            "line": line,
        }
        if existing:
            rid = existing["id"]
            path = f"/v2.1/zones/{self.zone_id}/recordsets/{rid}"
            payload = {"ttl": self.ttl, "records": ips}
            r = self._signed_request("PUT", path, body=payload)
            logger.info("update recordset line=%s ips=%s -> %s", line, ips, r.status_code)
        else:
            path = f"/v2.1/zones/{self.zone_id}/recordsets"
            r = self._signed_request("POST", path, body=body)
            logger.info("create recordset line=%s ips=%s -> %s", line, ips, r.status_code)
        if r.status_code >= 300:
            logger.error("huaweicloud dns API error: %s %s", r.status_code, r.text)

    def _find_recordset(self, line: str) -> Optional[dict]:
        path = f"/v2.1/zones/{self.zone_id}/recordsets"
        query = {"name": self.record_name, "type": "A", "line_id": line}
        r = self._signed_request("GET", path, query=query)
        if r.status_code != 200:
            logger.warning("list recordset failed: %s %s", r.status_code, r.text)
            return None
        rs = r.json().get("recordsets") or []
        for x in rs:
            if x.get("name") == self.record_name and x.get("type") == "A" and x.get("line") == line:
                return x
        return None

    # ------------------------------------------------------------------ signing
    def _signed_request(
        self,
        method: str,
        path: str,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> httpx.Response:
        """华为云 API 签名 v3 实现 (简化版, 仅覆盖本场景需要的请求)。"""
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body else ""
        now = _dt.datetime.utcnow()
        sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

        canonical_query = ""
        if query:
            items = sorted(query.items())
            canonical_query = "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in items)

        canonical_headers = (
            f"content-type:application/json\n"
            f"host:{self.host}\n"
            f"x-sdk-date:{sdk_date}\n"
        )
        signed_headers = "content-type;host;x-sdk-date"
        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        canonical_request = (
            f"{method.upper()}\n{path}\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{body_hash}"
        )
        string_to_sign = (
            f"{ALGORITHM}\n{sdk_date}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        signature = hmac.new(self.sk.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"{ALGORITHM} Access={self.ak}, SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Content-Type": "application/json",
            "Host": self.host,
            "X-Sdk-Date": sdk_date,
            "Authorization": authorization,
        }
        url = self.endpoint + path
        if canonical_query:
            url = f"{url}?{canonical_query}"
        with httpx.Client(timeout=15) as cli:
            return cli.request(method, url, headers=headers, content=body_str)
