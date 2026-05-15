"""
文本文件输出, 格式形如:

    162.159.43.99#CF优选-电信-日本
    104.16.1.2#CF优选-电信-日本
    ...

按 per_isp 模式时, 每个 ISP 一个文件; merged 模式合并为一个文件。
写入采用 "tmp + 原子 rename" 保证消费方不会读到半成品。
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Dict, List

from ..scoring import BestPick


logger = logging.getLogger(__name__)


class TextFileExporter:
    def __init__(
        self,
        output_dir: str,
        mode: str = "per_isp",
        line_template: str = "{ip}#CF优选-{isp_cn}-{region_cn}",
    ) -> None:
        self.output_dir = output_dir
        self.mode = mode
        self.line_template = line_template
        os.makedirs(output_dir, exist_ok=True)

    def export(self, picks_by_isp: Dict[str, List[BestPick]]) -> List[str]:
        written: List[str] = []
        if self.mode == "merged":
            path = os.path.join(self.output_dir, "cf_best.txt")
            lines: List[str] = []
            for isp, picks in picks_by_isp.items():
                for p in picks:
                    lines.append(self._render(isp, p))
            self._atomic_write(path, lines)
            written.append(path)
        else:
            for isp, picks in picks_by_isp.items():
                name = _safe_name(isp)
                path = os.path.join(self.output_dir, f"cf_best_{name}.txt")
                lines = [self._render(isp, p) for p in picks]
                self._atomic_write(path, lines)
                written.append(path)
        return written

    def _render(self, isp: str, p: BestPick) -> str:
        return self.line_template.format(
            ip=p.ip,
            isp_cn=isp,
            region_cn=p.region,
            colo=p.colo or "",
            latency=int(p.latency_ms),
            speed=int(p.speed_mbps),
        )

    def _atomic_write(self, path: str, lines: List[str]) -> None:
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
                if lines:
                    f.write("\n")
            os.replace(tmp, path)
            logger.info("wrote %s (%d lines)", path, len(lines))
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _safe_name(s: str) -> str:
    pinyin = {"电信": "telecom", "联通": "unicom", "移动": "mobile"}
    return pinyin.get(s, s)
