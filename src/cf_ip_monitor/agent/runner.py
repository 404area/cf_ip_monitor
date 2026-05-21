"""
Agent 主循环。

工作模式: 长轮询拉任务 -> 并发执行 -> 批量回报。
每个 VPS 上启动一个 runner, 通过命令行参数指定运营商。

容错:
- 网络异常 sleep idle_poll_interval 后重试
- 单条任务失败也照常回报 (用 ok=False), 让 master 沉淀沉默段
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

import httpx
import yaml

# 允许以 `python -m cf_ip_monitor.agent.runner` 启动, 也允许直接 `python runner.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cf_ip_monitor.shared.protocol import (  # noqa: E402
    AgentRegister,
    BatchAssignment,
    ProbeKind,
    ProbeResult,
    ProbeTask,
    ResultBatch,
    TraceHop,
)
from cf_ip_monitor.agent.probes.tcp_ping import tcp_ping  # noqa: E402
from cf_ip_monitor.agent.probes.http_trace import http_trace  # noqa: E402
from cf_ip_monitor.agent.probes.speed import speed_test  # noqa: E402
from cf_ip_monitor.agent.probes.traceroute import traceroute  # noqa: E402


logger = logging.getLogger(__name__)


def _resolve_env(value):
    """支持 ${ENV_NAME} 占位符。空值或非字符串原样返回。

    config.yaml 里如 master_url: "${AGENT_MASTER_URL}" 会被展开为对应环境变量。
    若环境变量未设置, 返回空串, 调用方再走默认值 fallback。
    """
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def now_ms() -> int:
    return int(time.time() * 1000)


def execute_task(task: ProbeTask) -> ProbeResult:
    """根据 task.kind 派发到对应 probe, 统一封装结果。"""
    ts = now_ms()
    try:
        if task.kind == ProbeKind.TCP_PING:
            stats = tcp_ping(
                task.ip, task.port, task.timeout_s,
                retry=task.retry, warmup=task.warmup,
            )
            return ProbeResult(
                task_id=task.task_id, ip=task.ip, kind=task.kind,
                ok=stats.ok,
                # 兼容字段: latency_ms 取 p50 (比 avg 稳定)
                latency_ms=stats.latency_p50,
                latency_min=stats.latency_min,
                latency_p50=stats.latency_p50,
                latency_p95=stats.latency_p95,
                latency_avg=stats.latency_avg,
                jitter_ms=stats.jitter_ms,
                samples=stats.samples,
                loss_rate=stats.loss_rate,
                error=stats.error,
                measured_at_ms=ts,
            )
        if task.kind == ProbeKind.HTTP_TRACE:
            ok, latency, colo, err = http_trace(
                task.ip, task.http_host, task.timeout_s,
            )
            return ProbeResult(
                task_id=task.task_id, ip=task.ip, kind=task.kind,
                ok=ok, latency_ms=latency, colo=colo, error=err,
                measured_at_ms=ts,
            )
        if task.kind == ProbeKind.SPEED:
            ok, mbps, downloaded, duration, err = speed_test(
                task.ip,
                host=task.http_host,
                bytes_to_download=task.speed_bytes or 50_000_000,
                timeout=task.speed_timeout_s or 15.0,
            )
            return ProbeResult(
                task_id=task.task_id, ip=task.ip, kind=task.kind,
                ok=ok, speed_mbps=mbps,
                bytes_downloaded=downloaded,
                duration_s=duration,
                error=err,
                measured_at_ms=ts,
            )
        if task.kind == ProbeKind.TRACEROUTE:
            ok, hops_raw, err = traceroute(
                task.ip,
                port=task.port or 443,
                max_hops=task.max_hops or 20,
                per_hop_timeout_s=task.per_hop_timeout_s or 2.0,
            )
            hops = [TraceHop(**h) for h in hops_raw]
            return ProbeResult(
                task_id=task.task_id, ip=task.ip, kind=task.kind,
                ok=ok, hops=hops, error=err,
                measured_at_ms=ts,
            )
        return ProbeResult(
            task_id=task.task_id, ip=task.ip, kind=task.kind,
            ok=False, error=f"unknown_kind:{task.kind}",
            measured_at_ms=ts,
        )
    except Exception as e:
        return ProbeResult(
            task_id=task.task_id, ip=task.ip, kind=task.kind,
            ok=False, error=f"exception:{e}",
            measured_at_ms=ts,
        )


class AgentRunner:
    def __init__(self, cfg: dict) -> None:
        a = cfg["agent"]
        # 优先级: CLI (在 main() 里塞回 cfg) > ${VAR} 展开 > 环境变量 > config 字面值 > 内置默认
        self.master_url = (
            _resolve_env(a.get("master_url"))
            or os.environ.get("AGENT_MASTER_URL")
            or "http://127.0.0.1:8088"
        ).rstrip("/")
        # token 降级链: config(${AGENT_AUTH_TOKEN}) > env AGENT_AUTH_TOKEN > env MASTER_AUTH_TOKEN
        # 同机 compose 场景下, 只需配 MASTER_AUTH_TOKEN, agent 也能用同一个 token 跑起来
        self.token = (
            _resolve_env(a.get("auth_token"))
            or os.environ.get("AGENT_AUTH_TOKEN")
            or os.environ.get("MASTER_AUTH_TOKEN")
            or ""
        )
        self.isp = (
            _resolve_env(a.get("isp"))
            or os.environ.get("AGENT_ISP")
            or ""
        )
        self.node_name = (
            _resolve_env(a.get("node_name"))
            or os.environ.get("AGENT_NODE_NAME")
            or ""
        )
        if not self.token:
            logger.warning("agent.auth_token 为空, 请通过 config.yaml 或 AGENT_AUTH_TOKEN 注入")
        if not self.isp or not self.node_name:
            raise SystemExit("agent.isp / agent.node_name 必须提供 (config.yaml / 环境变量 / CLI)")
        logger.info("agent target master_url=%s isp=%s node=%s", self.master_url, self.isp, self.node_name)
        self.concurrency = int(a.get("concurrency", 30))
        self.idle_poll = float(a.get("idle_poll_interval", 5))
        self.batch_max = int(a.get("batch_max", 100))
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=10.0),
            headers={"Authorization": f"Bearer {self.token}"},
        )
        # 把 tcp_timeout/tcp_retry/http_timeout 暴露给任务回填, 当 master 未指定时使用
        self.defaults = {
            "tcp_timeout": float(a.get("tcp_timeout", 2.0)),
            "tcp_retry": int(a.get("tcp_retry", 6)),
            "tcp_warmup": int(a.get("tcp_warmup", 1)),
            "http_timeout": float(a.get("http_timeout", 5.0)),
            "trace_max_hops": int(a.get("trace_max_hops", 20)),
            "trace_per_hop_timeout": float(a.get("trace_per_hop_timeout", 2.0)),
        }

    def register(self) -> None:
        body = AgentRegister(isp=self.isp, node_name=self.node_name).model_dump()
        try:
            r = self._client.post(f"{self.master_url}/v1/register", json=body)
            r.raise_for_status()
            logger.info("registered with master ok")
        except Exception as e:
            logger.warning("register failed (will retry on next pull): %s", e)

    def pull(self) -> List[ProbeTask]:
        body = {"isp": self.isp, "node_name": self.node_name, "max": self.batch_max}
        r = self._client.post(f"{self.master_url}/v1/tasks/pull", json=body)
        r.raise_for_status()
        ba = BatchAssignment.model_validate(r.json())
        for t in ba.tasks:
            if t.kind == ProbeKind.TCP_PING:
                if t.timeout_s <= 0:
                    t.timeout_s = self.defaults["tcp_timeout"]
                if t.retry <= 0:
                    t.retry = self.defaults["tcp_retry"]
                if t.warmup < 0:
                    t.warmup = self.defaults["tcp_warmup"]
            elif t.kind == ProbeKind.HTTP_TRACE:
                if t.timeout_s <= 0:
                    t.timeout_s = self.defaults["http_timeout"]
            elif t.kind == ProbeKind.TRACEROUTE:
                if t.max_hops <= 0:
                    t.max_hops = self.defaults["trace_max_hops"]
                if t.per_hop_timeout_s <= 0:
                    t.per_hop_timeout_s = self.defaults["trace_per_hop_timeout"]
        return ba.tasks

    def report(self, results: List[ProbeResult]) -> None:
        batch = ResultBatch(
            batch_id=str(now_ms()),
            isp=self.isp,
            node_name=self.node_name,
            results=results,
        )
        r = self._client.post(
            f"{self.master_url}/v1/tasks/report",
            json=batch.model_dump(mode="json"),
        )
        r.raise_for_status()

    def loop(self) -> None:
        self.register()
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while True:
                try:
                    tasks = self.pull()
                except Exception as e:
                    logger.warning("pull failed: %s", e)
                    time.sleep(self.idle_poll)
                    continue
                if not tasks:
                    time.sleep(self.idle_poll)
                    continue

                logger.info("got %d tasks", len(tasks))
                results = list(pool.map(execute_task, tasks))
                ok_n = sum(1 for r in results if r.ok)
                logger.info("done %d/%d ok", ok_n, len(results))

                try:
                    self.report(results)
                except Exception as e:
                    logger.warning("report failed: %s", e)
                    time.sleep(self.idle_poll)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--isp", help="覆盖配置文件的 isp 字段")
    parser.add_argument("--node-name", help="覆盖配置文件的 node_name 字段")
    parser.add_argument("--master-url", help="覆盖 master_url")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    cfg = load_config(args.config)
    if args.isp:
        cfg["agent"]["isp"] = args.isp
    if args.node_name:
        cfg["agent"]["node_name"] = args.node_name
    if args.master_url:
        cfg["agent"]["master_url"] = args.master_url

    AgentRunner(cfg).loop()


if __name__ == "__main__":
    main()
