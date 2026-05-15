#!/usr/bin/env python3
"""启动 Master (兼容入口, 推荐用 `cf-ip-master`)。

项目用 uv 管理, 通常直接调用 pyproject 注册的 console_script:
    uv run cf-ip-master --config config.yaml

本脚本仅保留给 PyCharm Run Configuration 等需要直接指向 .py 文件的场景。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_ip_monitor.master.server import main


if __name__ == "__main__":
    main()
