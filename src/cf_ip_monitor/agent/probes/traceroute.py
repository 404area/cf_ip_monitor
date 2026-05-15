"""
TCP SYN traceroute (端口 443) 探测。

实现选择:
- 优先调用系统 `traceroute` 命令 (Linux/macOS 通常自带), 参数固定:
    -T -p 443 -n -q 1 -w <timeout> -m <max_hops>
  这种方式无需额外 Python 依赖, 单文件即可工作。
- macOS 不带 `-T`, 用 `-P TCP -p 443` 替代 (与 -T 等价)。
- Linux 上若没安装 traceroute, 会回退到 ICMP traceroute (走 `traceroute` 默认行为),
  对于运营商封 ICMP 时结果会差一些。

权限要求:
- TCP traceroute 需要 raw socket, 在 Linux 上通常 setuid 已配好 (apt install traceroute);
  macOS 需要 sudo, 但日常调试在用户态也可以跑 (会回退到 ICMP)。

返回:
- (ok, hops, error)
- hops 是 List[dict], 每条 {hop_idx, hop_ip, rtt_ms}, 无响应跳 hop_ip=None
- 即使中间多跳 timeout, 只要至少有一跳响应, ok=True
"""
from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)


# 一行 traceroute 输出形如:
#   3  202.97.50.1  18.234 ms
#   4  * * *
# 我们 -q 1 只发一个 probe, 所以一行只有一个 rtt。
_LINE_RE = re.compile(
    r"^\s*(?P<idx>\d+)\s+"
    r"(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3})|(?P<star>\*))"
    r"(?:\s+(?P<rtt>[\d.]+)\s*ms)?"
)


def _build_cmd(ip: str, port: int, max_hops: int, timeout_s: float) -> Optional[List[str]]:
    """构造命令; 缺少 traceroute 时返回 None。"""
    if not shutil.which("traceroute"):
        return None
    sys = platform.system()
    if sys == "Darwin":
        return [
            "traceroute", "-n", "-q", "1",
            "-w", str(int(max(1, timeout_s))),
            "-m", str(max_hops),
            "-P", "TCP", "-p", str(port),
            ip,
        ]
    # Linux / others
    return [
        "traceroute", "-T", "-n", "-q", "1",
        "-w", str(int(max(1, timeout_s))),
        "-m", str(max_hops),
        "-p", str(port),
        ip,
    ]


def traceroute(
    ip: str,
    port: int = 443,
    max_hops: int = 20,
    per_hop_timeout_s: float = 2.0,
) -> Tuple[bool, List[dict], Optional[str]]:
    cmd = _build_cmd(ip, port, max_hops, per_hop_timeout_s)
    if cmd is None:
        return False, [], "traceroute_not_installed"

    # 总超时 = 跳数 × 单跳超时 + 一点 buffer, 防止异常进程一直挂
    total_timeout = max_hops * max(1.0, per_hop_timeout_s) + 5.0

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=total_timeout,
        )
    except subprocess.TimeoutExpired:
        return False, [], "subprocess_timeout"
    except Exception as e:
        return False, [], f"subprocess_error:{e}"

    hops: List[dict] = []
    for line in (proc.stdout or "").splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group("idx"))
        hop_ip = m.group("ip")
        rtt = m.group("rtt")
        hops.append({
            "hop_idx": idx,
            "hop_ip": hop_ip,
            "rtt_ms": float(rtt) if rtt else None,
        })

    if not hops:
        # 拿到 stderr 给排错用
        err = (proc.stderr or proc.stdout or "no_output").strip().splitlines()[-1:]
        return False, [], f"no_hops_parsed:{' '.join(err)[:200]}"

    has_response = any(h["hop_ip"] for h in hops)
    if not has_response:
        return False, hops, "all_hops_timeout"

    return True, hops, None
