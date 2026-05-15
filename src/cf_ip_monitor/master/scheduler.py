"""
后台调度器。

定时任务:
  - sample_round:     按 full_scan_hours 周期触发, 启动新轮采样
  - process_results:  高频触发, 把刚回报的采样结果聚合, 决定 alive/silent, 展开 alive 段
                      (B2: 改用 c_segment_state 的 sampled_round + expanded_round 事件驱动,
                       不再依赖 "最近 6 分钟" 时间窗, 因此 master 中断后能正确恢复)
  - speed_round:      在采样完成一段时间后, 对延迟最优的候选下发 speed 任务
  - trace_round:      对 best snapshot 中的 IP 每 trace_interval_hours 跑一次 traceroute
  - relabel_round:    根据最近 traceroute 数据刷新 ip_route_label
  - export_round:     把优选结果写入文本文件 + 推送 DNS
  - housekeeping:     回收僵尸 assignment + 清理 done 任务

轮次管理 (B3/B6):
  - 每次 sample_round 创建一个 scan_round_id (UUID), 入队任务都带这个 id
  - 触发新一轮前先检查: 上一轮如果 pending+assigned 还有任务, 跳过本次, 等下一周期
  - 全部 stage 跑完 (pending+assigned=0) 时 finish_round
"""
from __future__ import annotations

import collections
import logging
import time
import uuid
from typing import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from .labeling import relabel_recent
from .scoring import BestPick, Scorer
from .storage import Storage
from .strategy import Strategy


logger = logging.getLogger(__name__)


class MasterScheduler:
    def __init__(
        self,
        storage: Storage,
        strategy: Strategy,
        scorer: Scorer,
        isps: List[str],
        full_scan_hours: int,
        exporters: list,
        speed_top_percentile: float,
        speed_enabled: bool = True,
        speed_interval_minutes: int = 15,
        trace_enabled: bool = True,
        trace_interval_hours: int = 24,
        trace_top_n_per_isp: int = 200,
        cleanup_done_after_hours: int = 24,
    ) -> None:
        self.storage = storage
        self.strategy = strategy
        self.scorer = scorer
        self.isps = isps
        self.full_scan_hours = full_scan_hours
        self.exporters = exporters
        self.speed_top_percentile = speed_top_percentile
        self.speed_enabled = speed_enabled
        self.speed_interval_minutes = speed_interval_minutes
        self.trace_enabled = trace_enabled
        self.trace_interval_hours = trace_interval_hours
        self.trace_top_n_per_isp = trace_top_n_per_isp
        self.cleanup_done_after_hours = cleanup_done_after_hours
        self._sched = BackgroundScheduler(timezone="Asia/Shanghai")

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._sched.add_job(
            self._safe(self.run_sample_round),
            "interval", hours=self.full_scan_hours,
            id="sample_round", next_run_time=_now_plus(5),
        )
        self._sched.add_job(
            self._safe(self.run_process_results),
            "interval", minutes=2,
            id="process_results", next_run_time=_now_plus(30),
        )
        if self.speed_enabled:
            self._sched.add_job(
                self._safe(self.run_speed_round),
                "interval", minutes=self.speed_interval_minutes,
                id="speed_round", next_run_time=_now_plus(180),
            )
        else:
            logger.info("speed_test disabled: skipping speed_round registration")
        if self.trace_enabled:
            self._sched.add_job(
                self._safe(self.run_trace_round),
                "interval", hours=self.trace_interval_hours,
                id="trace_round", next_run_time=_now_plus(600),
            )
            self._sched.add_job(
                self._safe(self.run_relabel_round),
                "interval", hours=max(1, self.trace_interval_hours // 4),
                id="relabel_round", next_run_time=_now_plus(900),
            )
        else:
            logger.info("traceroute disabled: skipping trace_round/relabel_round")
        self._sched.add_job(
            self._safe(self.run_export_round),
            "interval", minutes=30,
            id="export_round", next_run_time=_now_plus(300),
        )
        self._sched.add_job(
            self._safe(self.run_housekeeping),
            "interval", minutes=5,
            id="housekeeping",
        )
        self._sched.start()
        logger.info(
            "scheduler started (speed_enabled=%s speed_interval=%smin "
            "trace_enabled=%s trace_interval=%sh full_scan=%sh)",
            self.speed_enabled, self.speed_interval_minutes,
            self.trace_enabled, self.trace_interval_hours, self.full_scan_hours,
        )

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    # ------------------------------------------------------------------ jobs
    def run_sample_round(self) -> None:
        """启动新一轮全量采样。

        B6: 上一轮还没跑完 (pending+assigned > 0) 时跳过, 防止两轮任务叠加。
        """
        prev = self.storage.current_round()
        if prev:
            left = self.storage.all_round_pending(prev)
            if left > 0:
                logger.warning(
                    "sample_round skipped: previous round %s still has %d tasks pending/assigned",
                    prev, left,
                )
                return
            # 上一轮所有任务都跑完了, 标记完成再开新的
            self.storage.finish_round(prev)
            logger.info("finished previous round %s", prev)

        round_id = uuid.uuid4().hex
        self.storage.create_round(round_id, notes=f"isps={','.join(self.isps)}")
        logger.info("==== sample_round: start round=%s ====", round_id)
        n = self.strategy.plan_initial_sampling(round_id)
        logger.info("sample_round enqueued %d tasks", n)

    def run_process_results(self) -> None:
        """事件驱动地处理"刚回报的采样结果"。

        步骤:
          1. 找出当前轮内 stage='sample' 已经全部 done 的 cidr (依赖回报时已聚合的
             c_segment_state, 由 server.report 那里入库时实时更新)。
          2. 对 c_segment_state.state='alive' 且 expanded_round != 当前轮的段, 触发展开。
          3. 同步把这些 cidr 内已经 TCP ok 的 IP 放入 http_trace 队列。

        和老版本的区别:
          - 不再依赖 "最近 6 分钟" 时间窗, master 中断后启动也能恢复未展开任务
          - 同一个 cidr 不会被展开两次 (claim_unexpanded_alive_segments 原子化)
        """
        round_id = self.storage.current_round()
        if not round_id:
            return

        alive = self.storage.claim_unexpanded_alive_segments(round_id, limit=5000)
        if not alive:
            return

        by_isp: Dict[str, List[tuple]] = collections.defaultdict(list)
        for isp, cidr in alive:
            by_isp[isp].append((isp, cidr))

        for isp, segs in by_isp.items():
            logger.info("isp=%s expanding %d alive segments (round=%s)",
                        isp, len(segs), round_id)
            self.strategy.expand_alive_segments(segs, round_id)
            # 同时把刚才采样阶段 TCP ok 的 IP 加 http_trace
            trace_targets = self._collect_sample_ok_ips(isp, [c for _, c in segs])
            if trace_targets:
                self.strategy.queue_http_trace(
                    [(isp, ip) for ip in trace_targets], round_id,
                )

    def _collect_sample_ok_ips(self, isp: str, cidrs: List[str]) -> List[str]:
        """从 probe_raw 里取出这些 cidr 内, 当前轮采样阶段 TCP ok 的 IP。"""
        if not cidrs:
            return []
        # 一次扫描表太重, 这里走 LIKE prefix 匹配 (cidr 是 a.b.c.0/24, prefix = a.b.c.)
        ips: List[str] = []
        with self.storage._conn() as c:
            for cidr in cidrs:
                prefix = cidr.split("/", 1)[0].rsplit(".", 1)[0] + "."
                cur = c.execute(
                    """SELECT DISTINCT ip FROM probe_raw
                       WHERE isp=? AND kind='tcp_ping' AND ok=1
                         AND stage='sample' AND ip LIKE ?""",
                    (isp, f"{prefix}%"),
                )
                ips.extend(r["ip"] for r in cur.fetchall())
        return ips

    def run_speed_round(self) -> None:
        """对每个 ISP 的低延迟候选下发 speed 任务。"""
        round_id = self.storage.current_round() or "ad-hoc"
        for isp in self.isps:
            cands = self.scorer.latency_top_candidates(isp, self.speed_top_percentile)
            if not cands:
                logger.info("speed_round isp=%s no candidates", isp)
                continue
            self.strategy.queue_speed_test([(isp, ip) for ip in cands], round_id)
            logger.info("speed_round isp=%s queued speed for %d ips", isp, len(cands))

    def run_trace_round(self) -> None:
        """对最近 best snapshot 中的 IP × 每个 ISP 跑 traceroute。

        范围控制 (D4):
          - 只取最近 trace_interval_hours × 2 内进入快照的 IP
          - 同一 (isp, ip) 在 trace_interval_hours 内已经 traceroute 过的跳过
          - 每个 ISP 最多取 trace_top_n_per_isp 个目标
        """
        round_id = self.storage.current_round() or "ad-hoc"
        since_ms = int(time.time() * 1000) - self.trace_interval_hours * 2 * 3600 * 1000
        for isp in self.isps:
            cand_ips = self.storage.best_snapshot_ips(isp, since_ms=since_ms)
            if not cand_ips:
                logger.info("trace_round isp=%s no snapshot ips", isp)
                continue
            already = self.storage.recent_trace_ips(
                isp, within_ms=self.trace_interval_hours * 3600 * 1000,
            )
            todo = [ip for ip in cand_ips if ip not in already]
            todo = todo[: self.trace_top_n_per_isp]
            if not todo:
                logger.info("trace_round isp=%s all up-to-date", isp)
                continue
            self.strategy.queue_traceroute([(isp, ip) for ip in todo], round_id)
            logger.info("trace_round isp=%s queued %d traceroute tasks", isp, len(todo))

    def run_relabel_round(self) -> None:
        n = relabel_recent(self.storage, self.isps,
                           within_hours=self.trace_interval_hours * 2)
        if n:
            logger.info("relabel_round updated %d (ip, isp) labels", n)

    def run_export_round(self) -> None:
        picks_by_isp: Dict[str, List[BestPick]] = {}
        snapshot_rows = []
        for isp in self.isps:
            picks = self.scorer.pick_best(isp)
            picks_by_isp[isp] = picks
            for p in picks:
                snapshot_rows.append(
                    (p.isp, p.region, p.ip, p.latency_ms, p.speed_mbps, p.score)
                )
        if snapshot_rows:
            self.storage.save_snapshot(snapshot_rows)
        for exp in self.exporters:
            try:
                exp.export(picks_by_isp)
            except Exception as e:
                logger.exception("exporter %s failed: %s", type(exp).__name__, e)

    def run_housekeeping(self) -> None:
        n = self.storage.requeue_stale_assignments(older_than_ms=15 * 60 * 1000)
        if n:
            logger.info("requeued %d stale assignments", n)
        cleaned = self.storage.cleanup_done_tasks(
            older_than_ms=self.cleanup_done_after_hours * 3600 * 1000,
        )
        if cleaned:
            logger.info("cleaned %d done tasks (>%dh)", cleaned, self.cleanup_done_after_hours)
        # 检查上一轮是否全部完成可以关闭
        cur = self.storage.current_round()
        if cur and self.storage.all_round_pending(cur) == 0:
            # 还要确保已经至少跑过一次 (避免刚启动 round 就被关掉)
            # 通过检查是否有任务属于这一轮判断
            with self.storage._conn() as c:
                total = c.execute(
                    "SELECT COUNT(*) AS n FROM task_queue WHERE scan_round_id=?",
                    (cur,),
                ).fetchone()["n"]
            if total > 0:
                self.storage.finish_round(cur)
                logger.info("finished round %s (all %d tasks done)", cur, total)

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _safe(fn):
        def wrapper():
            try:
                fn()
            except Exception as e:
                logger.exception("job %s failed: %s", fn.__name__, e)
        return wrapper


def _now_plus(seconds: int):
    import datetime as dt
    return dt.datetime.now() + dt.timedelta(seconds=seconds)
