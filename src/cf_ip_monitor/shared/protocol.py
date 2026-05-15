"""
Master <-> Agent 通信协议。

Agent 通过 HTTP 拉任务、回报结果，所有载荷使用 Pydantic 校验，便于版本演进时检测兼容性。

协议版本约定:
  v1: TCP_PING / HTTP_TRACE / SPEED, latency_ms 一个延迟字段
  v2: + TRACEROUTE 探测; ProbeResult 增加 latency_min/p50/p95/jitter/samples,
      ProbeTask 增加 warmup/max_hops 等参数
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


PROTOCOL_VERSION = "2"


class ProbeKind(str, Enum):
    """探测类型, 按代价从低到高排列。"""

    TCP_PING = "tcp_ping"
    HTTP_TRACE = "http_trace"
    SPEED = "speed"
    TRACEROUTE = "traceroute"


class ProbeTask(BaseModel):
    """单条探测任务。一个批次内允许混合不同 kind。"""

    task_id: str
    ip: str
    kind: ProbeKind
    # 通用细节参数, 由 master 下发, agent 透传使用
    port: int = 443
    timeout_s: float = 2.0
    retry: int = 6
    # TCP_PING: 在 retry 之外额外做 warmup 次握手, 结果不计入统计 (排除冷启动偏高)
    warmup: int = 1
    # SPEED 任务需要的额外参数
    speed_bytes: Optional[int] = None
    speed_timeout_s: Optional[float] = None
    # HTTP_TRACE / SPEED 用的 Host 头, 默认为 Cloudflare 官方
    http_host: str = "speed.cloudflare.com"
    # TRACEROUTE 参数
    max_hops: int = 20
    per_hop_timeout_s: float = 2.0


class BatchAssignment(BaseModel):
    """master -> agent: 一次性下发的任务包。"""

    batch_id: str
    assigned_at_ms: int
    tasks: List[ProbeTask] = Field(default_factory=list)


class TraceHop(BaseModel):
    """traceroute 单跳数据 (enrichment 由 master 端完成)。"""

    hop_idx: int
    hop_ip: Optional[str] = None              # None / "*" 表示该跳超时无响应
    rtt_ms: Optional[float] = None


class ProbeResult(BaseModel):
    """agent -> master: 单条任务的执行结果。失败也要回报, 便于训练沉默段策略。"""

    task_id: str
    ip: str
    kind: ProbeKind
    ok: bool
    # 通用兼容字段 (旧 master 仍可读懂): TCP_PING/HTTP_TRACE 的代表性 RTT
    # 对 TCP_PING 而言, 取值 = latency_p50, 不是简单 avg
    latency_ms: Optional[float] = None
    # TCP_PING 的分位/抖动统计 (v2)
    latency_min: Optional[float] = None
    latency_p50: Optional[float] = None
    latency_p95: Optional[float] = None
    latency_avg: Optional[float] = None
    jitter_ms: Optional[float] = None
    samples: Optional[int] = None
    loss_rate: Optional[float] = None         # 0~1, 失败次数 / 总尝试数
    colo: Optional[str] = None                # HTTP_TRACE 解出来的 Cloudflare colo 三字码
    speed_mbps: Optional[float] = None        # SPEED 的下行速率 MB/s
    bytes_downloaded: Optional[int] = None
    duration_s: Optional[float] = None
    # TRACEROUTE: 完整 hop 列表 (含超时跳)
    hops: Optional[List[TraceHop]] = None
    error: Optional[str] = None
    # Agent 本地时间戳, 用于回填时段维度
    measured_at_ms: int


class ResultBatch(BaseModel):
    """agent -> master: 多条结果打包回报。"""

    batch_id: str
    isp: str
    node_name: str
    results: List[ProbeResult] = Field(default_factory=list)


class AgentRegister(BaseModel):
    """agent 首次连接时上报自身信息, 仅用于 master 端日志/统计。"""

    isp: str
    node_name: str
    protocol_version: str = PROTOCOL_VERSION
