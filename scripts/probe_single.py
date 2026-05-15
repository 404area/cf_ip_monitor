#!/usr/bin/env python3
"""单机调试工具: 不依赖 master, 直接对一组 IP 跑全套探测, 立即输出结果。

例 (项目用 uv 管理):
  uv run python scripts/probe_single.py 162.159.43.99 104.16.1.2
  uv run python scripts/probe_single.py 162.159.43.99 --traceroute --enrich
  uv run python scripts/probe_single.py 162.159.43.99 --skip-speed --bytes 20000000

Docker 环境:
  docker compose exec master uv run python scripts/probe_single.py 162.159.43.99 --traceroute --enrich

输出: 每个 IP 一行 JSON, 含 tcp / trace / speed / traceroute / enrich 字段。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from cf_ip_monitor.agent.probes.tcp_ping import tcp_ping
from cf_ip_monitor.agent.probes.http_trace import http_trace
from cf_ip_monitor.agent.probes.speed import speed_test
from cf_ip_monitor.agent.probes.traceroute import traceroute
from cf_ip_monitor.agent.probes.icmp_ping import icmp_ping


def main():
    p = argparse.ArgumentParser()
    p.add_argument("ips", nargs="+")
    p.add_argument("--skip-speed", action="store_true")
    p.add_argument("--bytes", type=int, default=20_000_000)
    p.add_argument("--traceroute", action="store_true",
                   help="额外跑一次 traceroute (单 IP 5-15s, 较慢)")
    p.add_argument("--icmp", action="store_true",
                   help="额外跑一次 ICMP ping (跨 TCP 验证)")
    p.add_argument("--enrich", action="store_true",
                   help="对落地 IP 和 traceroute hop 做 enrichment (需要 ipdata 数据)")
    p.add_argument("--retry", type=int, default=6)
    p.add_argument("--warmup", type=int, default=1)
    args = p.parse_args()

    enricher = None
    if args.enrich:
        from cf_ip_monitor.master.enrich import Enricher
        enricher = Enricher.get()

    for ip in args.ips:
        out = {"ip": ip}
        stats = tcp_ping(ip, retry=args.retry, warmup=args.warmup)
        out["tcp"] = {
            "ok": stats.ok,
            "min": stats.latency_min,
            "p50": stats.latency_p50,
            "p95": stats.latency_p95,
            "avg": stats.latency_avg,
            "jitter": stats.jitter_ms,
            "samples": stats.samples,
            "loss": stats.loss_rate,
            "err": stats.error,
        }
        if args.icmp:
            ic = icmp_ping(ip, count=args.retry, warmup=args.warmup)
            out["icmp"] = {
                "ok": ic.ok, "min": ic.latency_min, "p50": ic.latency_p50,
                "p95": ic.latency_p95, "jitter": ic.jitter_ms,
                "samples": ic.samples, "loss": ic.loss_rate, "err": ic.error,
            }
        if stats.ok:
            ok2, rtt, colo, err2 = http_trace(ip)
            out["trace"] = {"ok": ok2, "rtt_ms": rtt, "colo": colo, "err": err2}
            if ok2 and not args.skip_speed:
                ok3, mbps, dl, dur, err3 = speed_test(ip, bytes_to_download=args.bytes)
                out["speed"] = {"ok": ok3, "mbps": mbps, "bytes": dl, "dur": dur, "err": err3}
        if args.traceroute:
            ok_t, hops, err_t = traceroute(ip)
            if enricher:
                for h in hops:
                    info = enricher.lookup(h.get("hop_ip"))
                    h["asn"] = info.asn
                    h["country"] = info.country
                    h["region"] = info.region
                    h["city"] = info.city
                    h["isp_cn"] = info.isp_cn
            out["traceroute"] = {"ok": ok_t, "err": err_t, "hops": hops}
        if enricher:
            info = enricher.lookup(ip)
            out["enrich"] = {
                "asn": info.asn, "as_name": info.as_name,
                "country": info.country, "region": info.region,
                "city": info.city, "isp_cn": info.isp_cn,
                "qqwry_raw": info.qqwry_raw,
            }
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
