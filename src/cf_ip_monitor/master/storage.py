"""
SQLite 存储层。

设计目标:
1. 探测结果按 (ip, isp, hour_bucket) 聚合，避免单 IP 历史无限膨胀
2. C 段沉默状态作为快速过滤条件单独存表
3. 优选输出快照可追溯
4. 引入 scan_round 表跟踪"一轮全量探测"的生命周期, 支持断点续传/进度查询
5. 路由 hop 单独表, IP 维度路径标签单独表

迁移策略:
- 老库 (没有新字段) 启动时自动 ALTER TABLE 补齐, 见 _migrate()
- 所有新加字段都设默认值, 老数据不需要回填
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple


logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ip            TEXT    NOT NULL,
    isp           TEXT    NOT NULL,
    node_name     TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    ok            INTEGER NOT NULL,
    latency_ms    REAL,                       -- 兼容字段: TCP_PING 时取 p50
    latency_min   REAL,
    latency_p50   REAL,
    latency_p95   REAL,
    latency_avg   REAL,
    jitter_ms     REAL,
    samples       INTEGER,
    loss_rate     REAL,
    colo          TEXT,
    speed_mbps    REAL,
    dst_asn       INTEGER,                    -- enrichment: 目标 IP 的 ASN
    dst_as_name   TEXT,
    dst_country   TEXT,
    dst_region    TEXT,
    dst_city      TEXT,
    measured_at   INTEGER NOT NULL,
    hour_bucket   INTEGER NOT NULL,
    scan_round_id TEXT,
    stage         TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_ip_isp_time  ON probe_raw(ip, isp, measured_at);
CREATE INDEX IF NOT EXISTS idx_raw_isp_hour     ON probe_raw(isp, hour_bucket);
CREATE INDEX IF NOT EXISTS idx_raw_measured     ON probe_raw(measured_at);
CREATE INDEX IF NOT EXISTS idx_raw_round        ON probe_raw(scan_round_id);

CREATE TABLE IF NOT EXISTS c_segment_state (
    -- key: <isp>|<24cidr>
    key            TEXT    PRIMARY KEY,
    isp            TEXT    NOT NULL,
    cidr           TEXT    NOT NULL,
    state          TEXT    NOT NULL,  -- 'alive' | 'silent' | 'unknown'
    last_checked   INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    sample_ok      INTEGER NOT NULL DEFAULT 0,
    sample_total   INTEGER NOT NULL DEFAULT 0,
    -- B2: 用于事件驱动的 alive->expand, 避免靠时间窗丢任务
    expanded_round TEXT,                      -- 哪一轮已经展开过
    sampled_round  TEXT                       -- 当前轮采样进度归属
);
CREATE INDEX IF NOT EXISTS idx_seg_isp_state ON c_segment_state(isp, state);

CREATE TABLE IF NOT EXISTS task_queue (
    task_id        TEXT    PRIMARY KEY,
    ip             TEXT    NOT NULL,
    isp            TEXT    NOT NULL,  -- 任务面向哪个运营商节点执行
    kind           TEXT    NOT NULL,
    stage          TEXT,               -- 'sample' | 'expand' | 'http_trace' | 'speed' | 'traceroute'
    scan_round_id  TEXT,
    payload_json   TEXT    NOT NULL,
    state          TEXT    NOT NULL,  -- 'pending' | 'assigned' | 'done'
    assigned_at    INTEGER,
    created_at     INTEGER NOT NULL,
    done_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_q_isp_state    ON task_queue(isp, state);
CREATE INDEX IF NOT EXISTS idx_q_round_stage  ON task_queue(scan_round_id, stage, state);
CREATE INDEX IF NOT EXISTS idx_q_done_at      ON task_queue(state, done_at);

CREATE TABLE IF NOT EXISTS scan_round (
    round_id       TEXT PRIMARY KEY,
    started_at     INTEGER NOT NULL,
    finished_at    INTEGER,
    state          TEXT NOT NULL,            -- 'running' | 'done' | 'aborted'
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_round_state ON scan_round(state);

CREATE TABLE IF NOT EXISTS best_ip_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at   INTEGER NOT NULL,
    isp           TEXT    NOT NULL,
    region        TEXT,
    ip            TEXT    NOT NULL,
    latency_p50   REAL,
    speed_p50     REAL,
    score         REAL
);
CREATE INDEX IF NOT EXISTS idx_best_isp_snap ON best_ip_snapshot(isp, snapshot_at);

CREATE TABLE IF NOT EXISTS probe_trace_hops (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ip           TEXT NOT NULL,               -- 目标 CF IP
    isp          TEXT NOT NULL,               -- 哪个 agent 上报的 (电信/联通/移动)
    node_name    TEXT NOT NULL,
    measured_at  INTEGER NOT NULL,
    hop_idx      INTEGER NOT NULL,
    hop_ip       TEXT,
    rtt_ms       REAL,
    asn          INTEGER,
    as_name      TEXT,
    country      TEXT,
    region       TEXT,
    city         TEXT,
    isp_cn       TEXT,                        -- ip2region.ISP 字段, 国内段 "电信/联通/移动/教育网"
    qqwry_raw    TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_ip_isp_time ON probe_trace_hops(ip, isp, measured_at);
CREATE INDEX IF NOT EXISTS idx_trace_asn         ON probe_trace_hops(asn);
CREATE INDEX IF NOT EXISTS idx_trace_measured    ON probe_trace_hops(measured_at);

CREATE TABLE IF NOT EXISTS ip_route_label (
    ip            TEXT NOT NULL,
    isp           TEXT NOT NULL,
    measured_at   INTEGER NOT NULL,
    line_type     TEXT,            -- 163/CN2-GT/CN2-GIA/4837/9929/9808/CMI/...
    exit_city     TEXT,            -- 国内最后一跳城市
    cn_hops       INTEGER,         -- 国内段跳数
    overseas_hops INTEGER,
    asn_path      TEXT,            -- 路径 ASN 序列, 逗号分隔; e.g. "4134,4134,13335"
    quality       REAL,            -- 0~1, 由 line_type 派生
    PRIMARY KEY (ip, isp)
);
CREATE INDEX IF NOT EXISTS idx_label_isp_line ON ip_route_label(isp, line_type);
"""


# 老库 -> 新库需要补的列 (列名, 类型, 默认 SQL 片段)
_MIGRATIONS_PROBE_RAW = [
    ("latency_min",  "REAL", ""),
    ("latency_p50",  "REAL", ""),
    ("latency_p95",  "REAL", ""),
    ("latency_avg",  "REAL", ""),
    ("jitter_ms",    "REAL", ""),
    ("samples",      "INTEGER", ""),
    ("dst_asn",      "INTEGER", ""),
    ("dst_as_name",  "TEXT", ""),
    ("dst_country",  "TEXT", ""),
    ("dst_region",   "TEXT", ""),
    ("dst_city",     "TEXT", ""),
    ("scan_round_id","TEXT", ""),
    ("stage",        "TEXT", ""),
]
_MIGRATIONS_TASK_QUEUE = [
    ("stage",         "TEXT", ""),
    ("scan_round_id", "TEXT", ""),
    ("done_at",       "INTEGER", ""),
]
_MIGRATIONS_C_SEGMENT = [
    ("expanded_round", "TEXT", ""),
    ("sampled_round",  "TEXT", ""),
]
_MIGRATIONS_TRACE_HOPS = [
    ("isp_cn", "TEXT", ""),
]


def _existing_columns(c: sqlite3.Connection, table: str) -> set:
    cur = c.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in (
        ("probe_raw", _MIGRATIONS_PROBE_RAW),
        ("task_queue", _MIGRATIONS_TASK_QUEUE),
        ("c_segment_state", _MIGRATIONS_C_SEGMENT),
        ("probe_trace_hops", _MIGRATIONS_TRACE_HOPS),
    ):
        try:
            existing = _existing_columns(c, table)
        except sqlite3.OperationalError:
            continue
        for name, typ, default in cols:
            if name in existing:
                continue
            sql = f"ALTER TABLE {table} ADD COLUMN {name} {typ}{(' DEFAULT ' + default) if default else ''}"
            try:
                c.execute(sql)
                logger.info("migrate: %s", sql)
            except sqlite3.OperationalError as e:
                logger.warning("migrate %s.%s failed: %s", table, name, e)


# ----------------------------------------------------------------- DTO
@dataclass
class RawResult:
    ip: str
    isp: str
    node_name: str
    kind: str
    ok: bool
    latency_ms: Optional[float]
    loss_rate: Optional[float]
    colo: Optional[str]
    speed_mbps: Optional[float]
    measured_at: int  # unix ms
    error: Optional[str]
    # v2 字段, 全部可选
    latency_min: Optional[float] = None
    latency_p50: Optional[float] = None
    latency_p95: Optional[float] = None
    latency_avg: Optional[float] = None
    jitter_ms: Optional[float] = None
    samples: Optional[int] = None
    dst_asn: Optional[int] = None
    dst_as_name: Optional[str] = None
    dst_country: Optional[str] = None
    dst_region: Optional[str] = None
    dst_city: Optional[str] = None
    scan_round_id: Optional[str] = None
    stage: Optional[str] = None


@dataclass
class SegmentAggregate:
    ip: str
    isp: str
    samples: int
    latency_p50: Optional[float]
    latency_p95: Optional[float]
    jitter_ms: Optional[float]
    speed_p50: Optional[float]
    colo: Optional[str]
    dst_asn: Optional[int] = None
    dst_country: Optional[str] = None
    dst_region: Optional[str] = None
    dst_city: Optional[str] = None


@dataclass
class TraceHopRow:
    ip: str
    isp: str
    node_name: str
    measured_at: int
    hop_idx: int
    hop_ip: Optional[str]
    rtt_ms: Optional[float]
    asn: Optional[int] = None
    as_name: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    isp_cn: Optional[str] = None
    qqwry_raw: Optional[str] = None


@dataclass
class RouteLabel:
    ip: str
    isp: str
    measured_at: int
    line_type: Optional[str]
    exit_city: Optional[str]
    cn_hops: int
    overseas_hops: int
    asn_path: str
    quality: float


@dataclass
class RoundProgress:
    round_id: str
    started_at: int
    finished_at: Optional[int]
    state: str
    by_stage: dict = field(default_factory=dict)


# ----------------------------------------------------------------- Storage
class Storage:
    """SQLite 操作封装。所有方法线程安全。"""

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db_path = db_path
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            _migrate(c)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.commit()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    # ------------------------------------------------------------------ writes
    def insert_results(self, items: Iterable[RawResult]) -> int:
        rows = []
        for r in items:
            rows.append((
                r.ip, r.isp, r.node_name, r.kind, 1 if r.ok else 0,
                r.latency_ms, r.latency_min, r.latency_p50, r.latency_p95,
                r.latency_avg, r.jitter_ms, r.samples,
                r.loss_rate, r.colo, r.speed_mbps,
                r.dst_asn, r.dst_as_name, r.dst_country, r.dst_region, r.dst_city,
                r.measured_at, _hour_bucket(r.measured_at),
                r.scan_round_id, r.stage, r.error,
            ))
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT INTO probe_raw
                (ip, isp, node_name, kind, ok,
                 latency_ms, latency_min, latency_p50, latency_p95,
                 latency_avg, jitter_ms, samples,
                 loss_rate, colo, speed_mbps,
                 dst_asn, dst_as_name, dst_country, dst_region, dst_city,
                 measured_at, hour_bucket,
                 scan_round_id, stage, error)
                VALUES (?,?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,
                        ?,?,?,
                        ?,?,?,?,?,
                        ?,?,
                        ?,?,?)""",
                rows,
            )
            c.commit()
        return len(rows)

    def insert_trace_hops(self, items: Iterable[TraceHopRow]) -> int:
        rows = [(
            r.ip, r.isp, r.node_name, r.measured_at, r.hop_idx,
            r.hop_ip, r.rtt_ms, r.asn, r.as_name,
            r.country, r.region, r.city, r.isp_cn, r.qqwry_raw,
        ) for r in items]
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT INTO probe_trace_hops
                (ip, isp, node_name, measured_at, hop_idx,
                 hop_ip, rtt_ms, asn, as_name,
                 country, region, city, isp_cn, qqwry_raw)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            c.commit()
        return len(rows)

    def upsert_route_label(self, label: RouteLabel) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO ip_route_label
                   (ip, isp, measured_at, line_type, exit_city,
                    cn_hops, overseas_hops, asn_path, quality)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ip, isp) DO UPDATE SET
                       measured_at=excluded.measured_at,
                       line_type=excluded.line_type,
                       exit_city=excluded.exit_city,
                       cn_hops=excluded.cn_hops,
                       overseas_hops=excluded.overseas_hops,
                       asn_path=excluded.asn_path,
                       quality=excluded.quality
                """,
                (label.ip, label.isp, label.measured_at, label.line_type,
                 label.exit_city, label.cn_hops, label.overseas_hops,
                 label.asn_path, label.quality),
            )
            c.commit()

    def get_route_labels(self, isp: str) -> dict:
        """返回 {ip: RouteLabel} 供 scoring 联表用。"""
        with self._conn() as c:
            cur = c.execute(
                "SELECT * FROM ip_route_label WHERE isp=?",
                (isp,),
            )
            out = {}
            for r in cur.fetchall():
                out[r["ip"]] = RouteLabel(
                    ip=r["ip"], isp=r["isp"], measured_at=r["measured_at"],
                    line_type=r["line_type"], exit_city=r["exit_city"],
                    cn_hops=r["cn_hops"] or 0, overseas_hops=r["overseas_hops"] or 0,
                    asn_path=r["asn_path"] or "", quality=r["quality"] or 0.0,
                )
            return out

    def set_segment_state(
        self,
        isp: str,
        cidr: str,
        state: str,
        ttl_seconds: int,
        sample_ok: int,
        sample_total: int,
        sampled_round: Optional[str] = None,
    ) -> None:
        now = int(time.time() * 1000)
        key = f"{isp}|{cidr}"
        with self._conn() as c:
            c.execute(
                """INSERT INTO c_segment_state
                   (key, isp, cidr, state, last_checked, expires_at,
                    sample_ok, sample_total, sampled_round)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       state=excluded.state,
                       last_checked=excluded.last_checked,
                       expires_at=excluded.expires_at,
                       sample_ok=excluded.sample_ok,
                       sample_total=excluded.sample_total,
                       sampled_round=excluded.sampled_round
                """,
                (key, isp, cidr, state, now, now + ttl_seconds * 1000,
                 sample_ok, sample_total, sampled_round),
            )
            c.commit()

    def mark_segment_expanded(self, isp: str, cidr: str, scan_round_id: str) -> None:
        key = f"{isp}|{cidr}"
        with self._conn() as c:
            c.execute(
                "UPDATE c_segment_state SET expanded_round=? WHERE key=?",
                (scan_round_id, key),
            )
            c.commit()

    def get_silent_segments(self, isp: str) -> List[str]:
        """返回当前对该 ISP 仍处于沉默状态且未过期的 /24 列表。"""
        now = int(time.time() * 1000)
        with self._conn() as c:
            cur = c.execute(
                "SELECT cidr FROM c_segment_state WHERE isp=? AND state='silent' AND expires_at>?",
                (isp, now),
            )
            return [r["cidr"] for r in cur.fetchall()]

    def enqueue_tasks(self, rows: List[Tuple]) -> int:
        """rows: List[(task_id, ip, isp, kind, stage, scan_round_id, payload_json)]"""
        if not rows:
            return 0
        now = int(time.time() * 1000)
        with self._conn() as c:
            c.executemany(
                """INSERT OR IGNORE INTO task_queue
                   (task_id, ip, isp, kind, stage, scan_round_id, payload_json, state, created_at)
                   VALUES (?,?,?,?,?,?,?, 'pending', ?)""",
                [(tid, ip, isp, kind, stage, round_id, payload, now)
                 for tid, ip, isp, kind, stage, round_id, payload in rows],
            )
            c.commit()
        return len(rows)

    def release_assigned_tasks(self, task_ids: List[str]) -> int:
        """将 assigned 任务退回 pending (pull 解析失败时使用)。"""
        if not task_ids:
            return 0
        with self._conn() as c:
            qmarks = ",".join("?" * len(task_ids))
            cur = c.execute(
                f"""UPDATE task_queue SET state='pending', assigned_at=NULL
                    WHERE task_id IN ({qmarks}) AND state='assigned'""",
                task_ids,
            )
            c.commit()
            return cur.rowcount

    def filter_ips_without_active_speed_tasks(
        self, isp: str, ips: List[str],
    ) -> List[str]:
        """过滤掉已有 pending/assigned speed 任务的 IP, 避免重复测速。"""
        if not ips:
            return []
        with self._conn() as c:
            # SQLite 变量数限制, 分批查
            busy: set = set()
            chunk = 400
            for i in range(0, len(ips), chunk):
                part = ips[i:i + chunk]
                qmarks = ",".join("?" * len(part))
                cur = c.execute(
                    f"""SELECT DISTINCT ip FROM task_queue
                       WHERE isp=? AND kind='speed'
                         AND state IN ('pending', 'assigned')
                         AND ip IN ({qmarks})""",
                    [isp, *part],
                )
                busy.update(r["ip"] for r in cur.fetchall())
        return [ip for ip in ips if ip not in busy]

    def claim_pending(self, isp: str, limit: int) -> List[sqlite3.Row]:
        """取一批 pending 任务并置为 assigned。
        优先级: stage 越靠前越先取 (sample > expand > http_trace > speed > traceroute)。
        """
        stage_order = ("sample", "expand", "http_trace", "speed", "traceroute")
        case_expr = " ".join(
            f"WHEN '{s}' THEN {i}" for i, s in enumerate(stage_order)
        )
        order_sql = f"ORDER BY CASE stage {case_expr} ELSE 99 END, created_at"
        now = int(time.time() * 1000)
        with self._conn() as c:
            try:
                cur = c.execute(
                    f"""UPDATE task_queue
                       SET state='assigned', assigned_at=?
                       WHERE task_id IN (
                           SELECT task_id FROM task_queue
                           WHERE isp=? AND state='pending'
                           {order_sql} LIMIT ?
                       )
                       RETURNING task_id, ip, kind, stage, scan_round_id, payload_json""",
                    (now, isp, limit),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                rows = c.execute(
                    "SELECT task_id, ip, kind, stage, scan_round_id, payload_json FROM task_queue "
                    f"WHERE isp=? AND state='pending' {order_sql} LIMIT ?",
                    (isp, limit),
                ).fetchall()
                if rows:
                    ids = [r["task_id"] for r in rows]
                    qmarks = ",".join("?" * len(ids))
                    c.execute(
                        f"UPDATE task_queue SET state='assigned', assigned_at=? "
                        f"WHERE task_id IN ({qmarks})",
                        [now, *ids],
                    )
            c.commit()
            return rows

    def mark_done(self, task_ids: List[str]) -> None:
        if not task_ids:
            return
        now = int(time.time() * 1000)
        qmarks = ",".join("?" * len(task_ids))
        with self._conn() as c:
            c.execute(
                f"UPDATE task_queue SET state='done', done_at=? WHERE task_id IN ({qmarks})",
                [now, *task_ids],
            )
            c.commit()

    def requeue_stale_assignments(self, older_than_ms: int) -> int:
        """把分配后超时未完成的任务重新置为 pending。"""
        cutoff = int(time.time() * 1000) - older_than_ms
        with self._conn() as c:
            cur = c.execute(
                "UPDATE task_queue SET state='pending', assigned_at=NULL "
                "WHERE state='assigned' AND assigned_at < ?",
                (cutoff,),
            )
            n = cur.rowcount
            c.commit()
            return n

    def cleanup_done_tasks(self, older_than_ms: int) -> int:
        """删除 N ms 之前已完成的任务, 防止 task_queue 长期膨胀。"""
        cutoff = int(time.time() * 1000) - older_than_ms
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM task_queue WHERE state='done' AND done_at IS NOT NULL AND done_at<?",
                (cutoff,),
            )
            n = cur.rowcount
            c.commit()
            return n

    def pending_count_by_isp(self) -> dict:
        with self._conn() as c:
            cur = c.execute(
                "SELECT isp, COUNT(*) AS n FROM task_queue WHERE state='pending' GROUP BY isp"
            )
            return {r["isp"]: r["n"] for r in cur.fetchall()}

    # ------------------------------------------------------------------ scan_round
    def create_round(self, round_id: str, notes: str = "") -> None:
        now = int(time.time() * 1000)
        with self._conn() as c:
            c.execute(
                """INSERT INTO scan_round (round_id, started_at, state, notes)
                   VALUES (?,?, 'running', ?)""",
                (round_id, now, notes),
            )
            c.commit()

    def current_round(self) -> Optional[str]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT round_id FROM scan_round WHERE state='running' "
                "ORDER BY started_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return row["round_id"] if row else None

    def finish_round(self, round_id: str) -> None:
        now = int(time.time() * 1000)
        with self._conn() as c:
            c.execute(
                "UPDATE scan_round SET state='done', finished_at=? WHERE round_id=?",
                (now, round_id),
            )
            c.commit()

    def round_progress(self, round_id: str) -> Optional[RoundProgress]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM scan_round WHERE round_id=?", (round_id,),
            ).fetchone()
            if not row:
                return None
            cur = c.execute(
                """SELECT stage, isp, state, COUNT(*) AS n
                   FROM task_queue WHERE scan_round_id=?
                   GROUP BY stage, isp, state""",
                (round_id,),
            )
            by_stage: dict = {}
            for r in cur.fetchall():
                stage = r["stage"] or "unknown"
                isp = r["isp"]
                st = r["state"]
                by_stage.setdefault(stage, {}).setdefault(isp, {})[st] = r["n"]
            return RoundProgress(
                round_id=round_id,
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                state=row["state"],
                by_stage=by_stage,
            )

    def list_rounds(self, limit: int = 10) -> List[dict]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT round_id, started_at, finished_at, state, notes "
                "FROM scan_round ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def all_round_pending(self, round_id: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "SELECT COUNT(*) AS n FROM task_queue "
                "WHERE scan_round_id=? AND state IN ('pending','assigned')",
                (round_id,),
            )
            return cur.fetchone()["n"]

    # ------------------------------------------------------------------ event-driven expand
    def sample_task_progress(
        self, isp: str, cidr: str, scan_round_id: str,
    ) -> Tuple[int, int]:
        """返回 (done, total) 采样阶段任务完成进度。"""
        with self._conn() as c:
            row = c.execute(
                """SELECT
                      SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done_n,
                      COUNT(*) AS total
                   FROM task_queue
                   WHERE isp=? AND stage='sample' AND scan_round_id=?
                     AND payload_json LIKE ?""",
                (isp, scan_round_id, f'%"cidr": "{cidr}"%'),
            ).fetchone()
            return (row["done_n"] or 0, row["total"] or 0)

    def sample_cidr_probe_summary(
        self, isp: str, cidr: str, scan_round_id: str,
    ) -> Tuple[int, int]:
        """返回 (ok_count, total_count) 本轮该 /24 采样探测结果汇总。"""
        prefix = cidr.split("/", 1)[0].rsplit(".", 1)[0] + "."
        with self._conn() as c:
            row = c.execute(
                """SELECT SUM(ok) AS ok_n, COUNT(*) AS total
                   FROM probe_raw
                   WHERE isp=? AND kind='tcp_ping' AND stage='sample'
                     AND scan_round_id=? AND ip LIKE ?""",
                (isp, scan_round_id, f"{prefix}%"),
            ).fetchone()
            return (row["ok_n"] or 0, row["total"] or 0)

    def claim_unexpanded_alive_segments(
        self, scan_round_id: str, limit: int = 5000,
    ) -> List[Tuple[str, str]]:
        """取出当前轮已 alive 但还没展开的段, 同时把 expanded_round 写上锁住,
        避免被并发触发两次。返回 [(isp, cidr), ...]
        """
        with self._conn() as c:
            cur = c.execute(
                """SELECT isp, cidr FROM c_segment_state
                   WHERE state='alive'
                     AND sampled_round=?
                     AND (expanded_round IS NULL OR expanded_round != ?)
                   LIMIT ?""",
                (scan_round_id, scan_round_id, limit),
            )
            rows = [(r["isp"], r["cidr"]) for r in cur.fetchall()]
            if rows:
                qmarks = ",".join(["(?,?)"] * len(rows))
                params: list = []
                for isp, cidr in rows:
                    params.extend([isp, cidr])
                # 把 expanded_round 标记为这轮
                c.executemany(
                    "UPDATE c_segment_state SET expanded_round=? WHERE isp=? AND cidr=?",
                    [(scan_round_id, isp, cidr) for isp, cidr in rows],
                )
                c.commit()
            return rows

    # ------------------------------------------------------------------ reads
    def aggregate_recent(
        self,
        isp: str,
        lookback_ms: int,
        same_hour_window: int,
    ) -> List[SegmentAggregate]:
        """聚合近期数据 (按当前小时±N 同时段加权样本)。

        - latency_p50 用 probe_raw.latency_p50 字段(新数据)的平均, 老数据回退到 latency_ms
        - jitter 取 jitter_ms 平均
        """
        now = int(time.time() * 1000)
        floor = now - lookback_ms
        cur_hour = (now // 3600000) % 24
        hour_set = {(cur_hour + offset) % 24 for offset in range(-same_hour_window, same_hour_window + 1)}
        hour_filter = ",".join(str(h) for h in hour_set)
        with self._conn() as c:
            cur = c.execute(
                f"""SELECT ip,
                           COALESCE(
                             (SELECT AVG(latency_ms) FROM probe_raw h
                                WHERE h.ip=probe_raw.ip AND h.isp=probe_raw.isp
                                  AND h.kind='http_trace' AND h.ok=1
                                  AND h.measured_at>=?),
                             AVG(CASE WHEN kind='tcp_ping' AND ok=1
                                      THEN COALESCE(latency_p50, latency_ms) END)
                           ) AS lat_p50,
                           AVG(CASE WHEN kind='tcp_ping' AND ok=1
                                    THEN latency_p95 END)                      AS lat_p95,
                           AVG(CASE WHEN kind='tcp_ping' AND ok=1
                                    THEN jitter_ms END)                        AS jit,
                           AVG(CASE WHEN kind='speed' AND ok=1
                                    THEN speed_mbps END)                       AS spd_avg,
                           SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END)                AS ok_cnt,
                           COUNT(*)                                             AS total,
                           (SELECT colo FROM probe_raw r2
                              WHERE r2.ip=probe_raw.ip AND r2.isp=probe_raw.isp
                                AND r2.colo IS NOT NULL
                              ORDER BY r2.measured_at DESC LIMIT 1)             AS last_colo,
                           (SELECT dst_asn FROM probe_raw r3
                              WHERE r3.ip=probe_raw.ip AND r3.isp=probe_raw.isp
                                AND r3.dst_asn IS NOT NULL
                              ORDER BY r3.measured_at DESC LIMIT 1)             AS dst_asn,
                           (SELECT dst_country FROM probe_raw r4
                              WHERE r4.ip=probe_raw.ip AND r4.isp=probe_raw.isp
                                AND r4.dst_country IS NOT NULL
                              ORDER BY r4.measured_at DESC LIMIT 1)             AS dst_country,
                           (SELECT dst_region FROM probe_raw r5
                              WHERE r5.ip=probe_raw.ip AND r5.isp=probe_raw.isp
                                AND r5.dst_region IS NOT NULL
                              ORDER BY r5.measured_at DESC LIMIT 1)             AS dst_region,
                           (SELECT dst_city FROM probe_raw r6
                              WHERE r6.ip=probe_raw.ip AND r6.isp=probe_raw.isp
                                AND r6.dst_city IS NOT NULL
                              ORDER BY r6.measured_at DESC LIMIT 1)             AS dst_city
                    FROM probe_raw
                    WHERE isp=? AND measured_at>=?
                      AND (hour_bucket % 24) IN ({hour_filter})
                    GROUP BY ip""",
                (isp, floor, floor),
            )
            out: List[SegmentAggregate] = []
            for r in cur.fetchall():
                out.append(SegmentAggregate(
                    ip=r["ip"], isp=isp,
                    samples=r["total"] or 0,
                    latency_p50=r["lat_p50"],
                    latency_p95=r["lat_p95"],
                    jitter_ms=r["jit"],
                    speed_p50=r["spd_avg"],
                    colo=r["last_colo"],
                    dst_asn=r["dst_asn"],
                    dst_country=r["dst_country"],
                    dst_region=r["dst_region"],
                    dst_city=r["dst_city"],
                ))
            return out

    def best_snapshot_ips(self, isp: str, since_ms: int) -> List[str]:
        """近 N 毫秒内进入 best 快照的 IP, 供 traceroute 选目标。"""
        with self._conn() as c:
            cur = c.execute(
                "SELECT DISTINCT ip FROM best_ip_snapshot "
                "WHERE isp=? AND snapshot_at>=?",
                (isp, since_ms),
            )
            return [r["ip"] for r in cur.fetchall()]

    def recent_trace_ips(self, isp: str, within_ms: int) -> set:
        """近 N 毫秒内已经 traceroute 过的 (isp, ip), 用于去重。"""
        cutoff = int(time.time() * 1000) - within_ms
        with self._conn() as c:
            cur = c.execute(
                "SELECT DISTINCT ip FROM probe_trace_hops "
                "WHERE isp=? AND measured_at>=?",
                (isp, cutoff),
            )
            return {r["ip"] for r in cur.fetchall()}

    def fetch_trace_hops(self, ip: str, isp: str, since_ms: int) -> List[dict]:
        """拿到最近一次 traceroute 的 hop 序列, 给 labeling 用。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(measured_at) AS t FROM probe_trace_hops "
                "WHERE ip=? AND isp=? AND measured_at>=?",
                (ip, isp, since_ms),
            ).fetchone()
            if not row or row["t"] is None:
                return []
            cur = c.execute(
                "SELECT * FROM probe_trace_hops "
                "WHERE ip=? AND isp=? AND measured_at=? ORDER BY hop_idx",
                (ip, isp, row["t"]),
            )
            return [dict(r) for r in cur.fetchall()]

    def save_snapshot(self, rows: List[Tuple[str, str, str, float, float, float]]) -> None:
        """rows: List[(isp, region, ip, latency_p50, speed_p50, score)]"""
        now = int(time.time() * 1000)
        with self._conn() as c:
            c.executemany(
                """INSERT INTO best_ip_snapshot
                   (snapshot_at, isp, region, ip, latency_p50, speed_p50, score)
                   VALUES (?,?,?,?,?,?,?)""",
                [(now, *r) for r in rows],
            )
            c.commit()


    def latest_best_snapshot(
        self,
        isp: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        """取最新一批 best_ip_snapshot (含路由标签), 用于 Web UI 展示。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(snapshot_at) AS t FROM best_ip_snapshot"
                + (" WHERE isp=?" if isp else ""),
                (isp,) if isp else (),
            ).fetchone()
            if not row or row["t"] is None:
                return []
            latest_ts = row["t"]
            window_ms = 30 * 60 * 1000
            query = (
                "SELECT s.snapshot_at, s.isp, s.region, s.ip, "
                "       s.latency_p50, s.speed_p50, s.score, "
                "       l.line_type, l.exit_city, l.quality "
                "FROM best_ip_snapshot s "
                "LEFT JOIN ip_route_label l ON s.ip=l.ip AND s.isp=l.isp "
                "WHERE s.snapshot_at >= ?"
                + (" AND s.isp=?" if isp else "")
                + " ORDER BY s.score DESC LIMIT ?"
            )
            params: list = [latest_ts - window_ms]
            if isp:
                params.append(isp)
            params.append(limit)
            cur = c.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def segment_summary(self) -> dict:
        """统计各 ISP 各 state 的 /24 段数量, 用于 Web UI 展示。"""
        now = int(time.time() * 1000)
        with self._conn() as c:
            cur = c.execute(
                """SELECT isp,
                          SUM(CASE WHEN state='alive' THEN 1 ELSE 0 END)                         AS alive,
                          SUM(CASE WHEN state='silent' AND expires_at >  ? THEN 1 ELSE 0 END)    AS silent,
                          SUM(CASE WHEN state='silent' AND expires_at <= ? THEN 1 ELSE 0 END)    AS expired,
                          COUNT(*) AS total
                   FROM c_segment_state GROUP BY isp""",
                (now, now),
            )
            return {r["isp"]: dict(r) for r in cur.fetchall()}

    def recent_probe_raw(
        self,
        limit: int = 100,
        offset: int = 0,
        isp: Optional[str] = None,
        kind: Optional[str] = None,
        ip_filter: Optional[str] = None,
    ) -> Tuple[List[dict], int]:
        """取最近 N 条探测明细, 用于 Web UI 展示。返回 (rows, total_matching)。"""
        conditions: List[str] = []
        params: list = []
        if isp:
            conditions.append("isp=?")
            params.append(isp)
        if kind:
            conditions.append("kind=?")
            params.append(kind)
        if ip_filter:
            conditions.append("ip LIKE ?")
            params.append(f"%{ip_filter}%")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._conn() as c:
            total = c.execute(
                f"SELECT COUNT(*) AS n FROM probe_raw {where}",
                params,
            ).fetchone()["n"]
            cur = c.execute(
                f"SELECT ip, isp, node_name, kind, ok, latency_ms, latency_p50, "
                f"latency_p95, jitter_ms, speed_mbps, colo, dst_country, dst_region, "
                f"stage, measured_at, error "
                f"FROM probe_raw {where} ORDER BY measured_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
            return [dict(r) for r in cur.fetchall()], total

    def probe_stats_summary(
        self,
        hours: int = 24,
        isps: Optional[List[str]] = None,
    ) -> dict:
        """近 N 小时各 ISP × kind 探测汇总统计, 用于 Web UI 展示。"""
        cutoff = int(time.time() * 1000) - hours * 3600 * 1000
        params: list = [cutoff]
        isp_filter = ""
        if isps:
            qmarks = ",".join("?" * len(isps))
            isp_filter = f" AND isp IN ({qmarks})"
            params.extend(isps)
        with self._conn() as c:
            cur = c.execute(
                f"""SELECT isp, kind,
                           COUNT(*) AS total,
                           SUM(ok)  AS ok_n,
                           AVG(CASE WHEN ok=1
                                    THEN COALESCE(
                                      CASE WHEN kind='http_trace' THEN latency_ms END,
                                      latency_p50, latency_ms
                                    ) END) AS avg_lat,
                           AVG(CASE WHEN ok=1 AND kind='speed' THEN speed_mbps END) AS avg_spd
                    FROM probe_raw WHERE measured_at>=?{isp_filter}
                    GROUP BY isp, kind""",
                params,
            )
            out: dict = {}
            for r in cur.fetchall():
                out.setdefault(r["isp"], {})[r["kind"]] = {
                    "total": r["total"],
                    "ok": r["ok_n"] or 0,
                    "avg_lat_ms": round(r["avg_lat"], 1) if r["avg_lat"] else None,
                    "avg_speed_mbps": round(r["avg_spd"], 1) if r["avg_spd"] else None,
                }
            return out


def _hour_bucket(ts_ms: int) -> int:
    """把 unix ms 转为按小时归桶, 便于做时段聚合。"""
    return ts_ms // 3600000
