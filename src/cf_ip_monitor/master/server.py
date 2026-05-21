"""
Master HTTP 入口。

Endpoints:
  POST /v1/register         agent 启动时上报身份
  POST /v1/tasks/pull       agent 长轮询拉任务, body: {isp, node_name, max}
  POST /v1/tasks/report     agent 回报结果, body: ResultBatch
  GET  /v1/stats            简单统计 (调试用)
  GET  /v1/round/current    当前轮次的进度详情
  GET  /v1/round/list       近 N 轮的概要
  GET  /healthz             健康检查

report 处理增强:
  - 落地 IP 自动 enrichment (ASN/country/region/city)
  - TRACEROUTE 结果按 hop 入 probe_trace_hops, 每跳同步 enrichment
  - TCP_PING sample 结果按 cidr 实时聚合到 c_segment_state, 让 alive->expand 事件驱动
"""
from __future__ import annotations

import collections
import json
import logging
import os
import time as _t
import uuid
from typing import Dict, List, Optional, Tuple

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..ipdata import available as _ipdata_avail
from ..shared.protocol import (
    AgentRegister,
    BatchAssignment,
    ProbeKind,
    ProbeResult,
    ProbeTask,
    ResultBatch,
)
from .enrich import Enricher
from .exporters.huaweicloud import HuaweiCloudDNSExporter
from .exporters.text_file import TextFileExporter
from .scheduler import MasterScheduler
from .scoring import Scorer, ScoringConfig
from .storage import RawResult, Storage, TraceHopRow
from .strategy import Strategy, StrategyConfig


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- request DTO
class PullRequest(BaseModel):
    isp: str
    node_name: str
    max: int = 100


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_speed_test_cfg(mcfg: dict) -> dict:
    """
    把测速相关配置统一收口到 master.speed_test 块下, 同时兼容旧字段。
    """
    st = dict(mcfg.get("speed_test") or {})
    sch = mcfg.get("scheduler") or {}
    sco = mcfg.get("scoring") or {}

    def _pick(key_new, key_old_section, key_old, default):
        if key_new in st:
            return st[key_new]
        old = key_old_section.get(key_old)
        if old is not None:
            logger.warning(
                "config: master.%s.%s is deprecated, move to master.speed_test.%s",
                "scheduler" if key_old_section is sch else "scoring",
                key_old, key_new,
            )
            return old
        return default

    return {
        "enabled":          bool(st.get("enabled", True)),
        "interval_minutes": int(st.get("interval_minutes", 15)),
        "top_percentile":   float(_pick("top_percentile",   sch, "speed_test_top_percentile", 0.2)),
        "bytes":            int(_pick("bytes",              sch, "speed_test_bytes",          50_000_000)),
        "timeout_s":        float(_pick("timeout_s",        sch, "speed_test_timeout",        15.0)),
        "min_speed_mbps":   float(_pick("min_speed_mbps",   sco, "min_speed_mbps",            10.0)),
    }


def _resolve_trace_cfg(mcfg: dict) -> dict:
    tr = dict(mcfg.get("traceroute") or {})
    return {
        "enabled":           bool(tr.get("enabled", True)),
        "interval_hours":    int(tr.get("interval_hours", 24)),
        "top_n_per_isp":     int(tr.get("top_n_per_isp", 200)),
        "max_hops":          int(tr.get("max_hops", 20)),
        "per_hop_timeout_s": float(tr.get("per_hop_timeout_s", 2.0)),
    }


def build_app(config_path: str) -> FastAPI:
    cfg = load_config(config_path)
    mcfg = cfg["master"]
    # auth_token 支持 ${VAR} 占位符 + 环境变量降级
    raw_token = mcfg.get("auth_token", "")
    if isinstance(raw_token, str) and raw_token.startswith("${") and raw_token.endswith("}"):
        raw_token = os.environ.get(raw_token[2:-1], "")
    auth_token = raw_token or os.environ.get("MASTER_AUTH_TOKEN", "")
    if not auth_token:
        logger.warning("master.auth_token 为空, /v1/* 接口将拒绝所有请求; 请配置 config.yaml 或注入 MASTER_AUTH_TOKEN")

    storage = Storage(mcfg["database"]["path"])
    isps = list(mcfg.get("agent_isps") or ["电信", "联通", "移动"])

    # 预初始化 enricher (确保 mmdb / qqwry 文件存在)
    enricher = Enricher.get()
    logger.info("ipdata: %s", _ipdata_avail())

    st_cfg = _resolve_speed_test_cfg(mcfg)
    tr_cfg = _resolve_trace_cfg(mcfg)
    logger.info(
        "speed_test: enabled=%s interval=%smin top=%.0f%% bytes=%d timeout=%.1fs min_speed=%.1fMB/s",
        st_cfg["enabled"], st_cfg["interval_minutes"],
        st_cfg["top_percentile"] * 100, st_cfg["bytes"],
        st_cfg["timeout_s"], st_cfg["min_speed_mbps"],
    )
    logger.info(
        "traceroute: enabled=%s interval=%sh top_n_per_isp=%d max_hops=%d",
        tr_cfg["enabled"], tr_cfg["interval_hours"],
        tr_cfg["top_n_per_isp"], tr_cfg["max_hops"],
    )

    strat_cfg = StrategyConfig(
        isps=isps,
        c_segment_probe_offsets=mcfg["scheduler"]["c_segment_probe_offsets"],
        c_segment_silent_ttl_hours=mcfg["scheduler"]["c_segment_silent_ttl_hours"],
        speed_test_bytes=st_cfg["bytes"],
        speed_test_timeout_s=st_cfg["timeout_s"],
        trace_max_hops=tr_cfg["max_hops"],
        trace_per_hop_timeout_s=tr_cfg["per_hop_timeout_s"],
    )
    strategy = Strategy(storage, strat_cfg)

    score_cfg = ScoringConfig(
        min_speed_mbps=st_cfg["min_speed_mbps"],
        max_latency_ms=mcfg["scoring"]["max_latency_ms"],
        top_n_per_bucket=mcfg["scoring"]["top_n_per_bucket"],
        lookback_days=mcfg["scoring"]["lookback_days"],
        same_hour_window=mcfg["scoring"]["same_hour_window"],
        speed_required=st_cfg["enabled"],
        max_jitter_ms=float(mcfg["scoring"].get("max_jitter_ms", 200.0)),
        route_quality_enabled=bool(mcfg["scoring"].get("route_quality_enabled", True)),
    )
    scorer = Scorer(storage, score_cfg)

    exporters = []
    ex_cfg = mcfg["exporters"]
    if ex_cfg.get("text_file", {}).get("enabled"):
        tf = ex_cfg["text_file"]
        exporters.append(TextFileExporter(
            output_dir=tf["output_dir"],
            mode=tf.get("mode", "per_isp"),
            line_template=tf.get("line_template", "{ip}#CF优选-{isp_cn}-{region_cn}"),
        ))
    if ex_cfg.get("huaweicloud_dns", {}).get("enabled"):
        hc = ex_cfg["huaweicloud_dns"]
        exporters.append(HuaweiCloudDNSExporter(
            ak=hc["ak"], sk=hc["sk"], region=hc["region"],
            zone_id=hc["zone_id"], record_name=hc["record_name"],
            ttl=hc.get("ttl", 60),
            isp_line_map=hc["isp_line_map"],
            records_per_line=hc.get("records_per_line", 5),
        ))

    sched = MasterScheduler(
        storage=storage,
        strategy=strategy,
        scorer=scorer,
        isps=isps,
        full_scan_hours=mcfg["scheduler"]["full_scan_hours"],
        exporters=exporters,
        speed_top_percentile=st_cfg["top_percentile"],
        speed_enabled=st_cfg["enabled"],
        speed_interval_minutes=st_cfg["interval_minutes"],
        trace_enabled=tr_cfg["enabled"],
        trace_interval_hours=tr_cfg["interval_hours"],
        trace_top_n_per_isp=tr_cfg["top_n_per_isp"],
        cleanup_done_after_hours=int(mcfg["scheduler"].get("cleanup_done_after_hours", 24)),
    )

    app = FastAPI(title="CF IP Monitor Master")

    def _check_token(authorization: str = Header(...)) -> None:
        expected = f"Bearer {auth_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="bad token")

    @app.on_event("startup")
    def _on_start() -> None:
        logger.info("startup: ISPs=%s exporters=%s", isps, [type(e).__name__ for e in exporters])
        sched.start()

    @app.on_event("shutdown")
    def _on_stop() -> None:
        sched.shutdown()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/v1/register")
    def register(body: AgentRegister, _=Depends(_check_token)):
        logger.info("agent register: %s/%s v%s", body.isp, body.node_name, body.protocol_version)
        return {"ok": True}

    @app.post("/v1/tasks/pull", response_model=BatchAssignment)
    def pull(body: PullRequest, _=Depends(_check_token)) -> BatchAssignment:
        max_n = max(1, min(body.max, mcfg["scheduler"]["batch_size"]))
        rows = storage.claim_pending(body.isp, max_n)
        tasks: List[ProbeTask] = []
        for r in rows:
            try:
                p = json.loads(r["payload_json"])
            except Exception:
                continue
            kind = ProbeKind(p.get("kind", r["kind"]))
            tasks.append(ProbeTask(
                task_id=r["task_id"],
                ip=p["ip"],
                kind=kind,
                port=p.get("port", 443),
                timeout_s=p.get("timeout_s", 2.0),
                retry=p.get("retry", 6),
                warmup=p.get("warmup", 1),
                speed_bytes=p.get("speed_bytes"),
                speed_timeout_s=p.get("speed_timeout_s"),
                http_host=p.get("http_host", "speed.cloudflare.com"),
                max_hops=p.get("max_hops", tr_cfg["max_hops"]),
                per_hop_timeout_s=p.get("per_hop_timeout_s", tr_cfg["per_hop_timeout_s"]),
            ))
        return BatchAssignment(
            batch_id=uuid.uuid4().hex,
            assigned_at_ms=int(_t.time() * 1000),
            tasks=tasks,
        )

    def _lookup_task_meta(task_ids: List[str]) -> Dict[str, dict]:
        """从 task_queue 反查每个 task_id 的 stage/cidr/scan_round_id, 用于回填。"""
        if not task_ids:
            return {}
        out: Dict[str, dict] = {}
        with storage._conn() as c:
            qmarks = ",".join("?" * len(task_ids))
            cur = c.execute(
                f"SELECT task_id, stage, scan_round_id, payload_json FROM task_queue "
                f"WHERE task_id IN ({qmarks})",
                task_ids,
            )
            for row in cur.fetchall():
                cidr = None
                try:
                    p = json.loads(row["payload_json"])
                    cidr = p.get("cidr")
                except Exception:
                    pass
                out[row["task_id"]] = {
                    "stage": row["stage"],
                    "scan_round_id": row["scan_round_id"],
                    "cidr": cidr,
                }
        return out

    def _bump_segment_state(
        isp: str,
        sample_results_by_cidr: Dict[str, List[bool]],
        scan_round_id: Optional[str],
    ) -> None:
        """sample 阶段每收一批就尝试更新 c_segment_state: alive / silent。

        我们没办法 "原子地知道一个 cidr 的所有 sample task 都回来了",
        因此采用 "宽容策略": 只要任意一次回报里出现了 ok=True, 就立刻标 alive;
        全部失败的 cidr 等到该 cidr 所有 sample 任务都 done 后再标 silent。
        """
        if not sample_results_by_cidr:
            return
        ttl_s = mcfg["scheduler"]["c_segment_silent_ttl_hours"] * 3600

        # 先把出现了 ok 的 cidr 直接 alive
        for cidr, oks in sample_results_by_cidr.items():
            if any(oks):
                storage.set_segment_state(
                    isp, cidr, "alive", ttl_s,
                    sample_ok=sum(1 for x in oks if x),
                    sample_total=len(oks),
                    sampled_round=scan_round_id,
                )

        # 对没有 ok 的 cidr, 检查这一轮该 cidr 的 sample 任务是否全部 done
        if scan_round_id:
            with storage._conn() as c:
                for cidr, oks in sample_results_by_cidr.items():
                    if any(oks):
                        continue
                    row = c.execute(
                        """SELECT
                              SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done_n,
                              COUNT(*) AS total
                           FROM task_queue
                           WHERE isp=? AND stage='sample' AND scan_round_id=?
                             AND payload_json LIKE ?""",
                        (isp, scan_round_id, f'%"cidr": "{cidr}"%'),
                    ).fetchone()
                    if not row or not row["total"]:
                        continue
                    if row["done_n"] == row["total"]:
                        storage.set_segment_state(
                            isp, cidr, "silent", ttl_s,
                            sample_ok=0, sample_total=row["total"],
                            sampled_round=scan_round_id,
                        )

    @app.post("/v1/tasks/report")
    def report(body: ResultBatch, _=Depends(_check_token)):
        ids = [r.task_id for r in body.results]
        meta = _lookup_task_meta(ids)

        raw_items: List[RawResult] = []
        sample_by_cidr: Dict[str, List[bool]] = collections.defaultdict(list)
        traces: List[Tuple[ProbeResult, dict]] = []
        scan_round_id: Optional[str] = None

        for r in body.results:
            m = meta.get(r.task_id, {})
            stage = m.get("stage")
            round_id = m.get("scan_round_id")
            if round_id and not scan_round_id:
                scan_round_id = round_id

            # enrichment: 落地 IP (CF IP)
            info = enricher.lookup(r.ip)

            raw_items.append(RawResult(
                ip=r.ip, isp=body.isp, node_name=body.node_name,
                kind=r.kind.value, ok=r.ok,
                latency_ms=r.latency_ms,
                latency_min=r.latency_min,
                latency_p50=r.latency_p50,
                latency_p95=r.latency_p95,
                latency_avg=r.latency_avg,
                jitter_ms=r.jitter_ms,
                samples=r.samples,
                loss_rate=r.loss_rate,
                colo=r.colo, speed_mbps=r.speed_mbps,
                dst_asn=info.asn,
                dst_as_name=info.as_name,
                dst_country=info.country,
                dst_region=info.region,
                dst_city=info.city,
                measured_at=r.measured_at_ms,
                error=r.error,
                scan_round_id=round_id,
                stage=stage,
            ))

            # 累积 sample 阶段结果, 喂给段状态机
            if r.kind == ProbeKind.TCP_PING and stage == "sample":
                cidr = m.get("cidr")
                if cidr:
                    sample_by_cidr[cidr].append(bool(r.ok))

            if r.kind == ProbeKind.TRACEROUTE and r.hops:
                traces.append((r, m))

        n = storage.insert_results(raw_items)
        storage.mark_done(ids)

        # traceroute hops 入库, 每跳富化
        hop_rows: List[TraceHopRow] = []
        for r, m in traces:
            for h in (r.hops or []):
                hi = enricher.lookup(h.hop_ip) if h.hop_ip else None
                hop_rows.append(TraceHopRow(
                    ip=r.ip, isp=body.isp, node_name=body.node_name,
                    measured_at=r.measured_at_ms,
                    hop_idx=h.hop_idx,
                    hop_ip=h.hop_ip, rtt_ms=h.rtt_ms,
                    asn=hi.asn if hi else None,
                    as_name=hi.as_name if hi else None,
                    country=hi.country if hi else None,
                    region=hi.region if hi else None,
                    city=hi.city if hi else None,
                    isp_cn=hi.isp_cn if hi else None,
                    qqwry_raw=hi.qqwry_raw if hi else None,
                ))
        nh = storage.insert_trace_hops(hop_rows)

        # 段状态机更新 (event-driven)
        if sample_by_cidr:
            _bump_segment_state(body.isp, sample_by_cidr, scan_round_id)

        return {"stored": n, "hops_stored": nh}

    @app.get("/v1/stats")
    def stats(_=Depends(_check_token)):
        return {
            "pending_by_isp": storage.pending_count_by_isp(),
            "current_round": storage.current_round(),
        }

    @app.get("/v1/round/current")
    def round_current(_=Depends(_check_token)):
        rid = storage.current_round()
        if not rid:
            return {"round_id": None}
        prog = storage.round_progress(rid)
        return prog.__dict__ if prog else {"round_id": rid}

    @app.get("/v1/round/list")
    def round_list(limit: int = 10, _=Depends(_check_token)):
        return {"rounds": storage.list_rounds(limit=limit)}

    return app


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    app = build_app(args.config)
    cfg = load_config(args.config)["master"]
    uvicorn.run(app, host=cfg["host"], port=cfg["port"])


if __name__ == "__main__":
    main()
