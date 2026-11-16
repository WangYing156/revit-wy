"""把 tools/mock_revit.py 变成一条命令：没有 Revit 也能完整试跑一遍。

为什么值得做成一等命令
----------------------
现场最常见的第一次反馈是"我没有 Revit / Revit 没装好，试不了"。
但本项目 90% 的价值（读图、清洗、对照、构件清单、差异、减负账、可视化）
根本不需要 Revit。缺的只是一个肯回包的"假 Revit"。

这里有两条路：
  · `revit-wy demo`  —— 前台：起假 Revit + 跑一遍完整流程 + 打印说明书
  · `revit-wy mock`  —— 后台：只起假 Revit，供 CLI / MCP / 工作台连接

假 Revit 只做协议级仿真：校验 IR 字段、按真实插件的错误码回包、统计构件。
它不会生成真实几何——所以"能不能建对"仍然要在真 Revit 上验。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional


def _load_mock():
    tools = Path(__file__).resolve().parent.parent.parent / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import mock_revit
    return mock_revit


def start_background(port: int = 48880, host: str = "127.0.0.1") -> Any:
    """在后台线程起假 Revit，返回 server 对象（调用方负责 shutdown）。"""
    mr = _load_mock()
    srv = mr.Server((host, port), mr.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(20):
        time.sleep(0.05)
        if port_open(port):
            break
    return srv


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def serve_forever(port: int = 48880, host: str = "127.0.0.1") -> None:
    mr = _load_mock()
    mr.serve(port, host)
