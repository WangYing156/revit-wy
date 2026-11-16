"""Python ↔ Revit 2022+ 插件 的进程间通信。

为什么不用 stdio 直连 Revit
--------------------------
Revit 插件是加载在 Revit.exe 内的 DLL，无法充当 stdio 子进程（会阻塞 UI 线程）。
业界通行做法（revit-mcp 生态）是三段式：

    MCP Client ──stdio(MCP)──▶ Python MCP Server ──TCP(NDJSON)──▶ Revit Add-in

本模块实现第三段：NDJSON over TCP（一行一条 JSON-RPC 2.0 风格消息）。
选 TCP 而非 WebSocket 的原因：.NET Framework 4.8 自带 TcpListener，
插件端零第三方依赖，部署到设计院受限网络环境最省事。
"""
from __future__ import annotations

import json
import socket
import time
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48880          # 选高位端口，避开 8080/60100 等常见占用
PROTOCOL = "revit-wy/1"


class BridgeError(RuntimeError):
    pass


class RevitBridge:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 300.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    # ---------------- 连接 ----------------
    def connect(self, retries: int = 3, delay: float = 1.0) -> None:
        last: Optional[Exception] = None
        for i in range(retries):
            try:
                s = socket.create_connection((self.host, self.port), timeout=10)
                s.settimeout(self.timeout)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = s
                self._buf = b""
                return
            except OSError as ex:
                last = ex
                if i < retries - 1:
                    time.sleep(delay)
        raise BridgeError(
            f"无法连接 Revit 插件 {self.host}:{self.port}。"
            f"请确认：① Revit 已打开目标项目；② 插件已加载（revit-wy 面板显示 Listening）；"
            f"③ 端口一致。底层错误：{last}"
        )

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ---------------- 收发 ----------------
    def _send(self, obj: Dict[str, Any]) -> None:
        if not self._sock:
            self.connect()
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        assert self._sock is not None
        self._sock.sendall(data)

    def _recv_line(self) -> Dict[str, Any]:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise BridgeError("Revit 插件已断开连接")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            return self._recv_line()
        return json.loads(line.decode("utf-8"))

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """同步调用；返回 result，异常时抛 BridgeError。"""
        rid = uuid.uuid4().hex
        if timeout and self._sock:
            self._sock.settimeout(timeout)
        self._send({
            "protocol": PROTOCOL, "id": rid, "method": method,
            "params": params or {},
        })
        while True:
            msg = self._recv_line()
            if msg.get("id") not in (rid, None):
                continue
            if msg.get("event"):           # 进度事件
                continue
            if msg.get("error"):
                err = msg["error"]
                raise BridgeError(f"{err.get('code','ERR')}: {err.get('message','')}")
            return msg.get("result", {})

    # ---------------- 业务便捷方法 ----------------
    def ping(self) -> Dict[str, Any]:
        return self.call("ping")

    def status(self) -> Dict[str, Any]:
        return self.call("status")

    def prepare_project(self, template: str = "", units_mm: bool = True,
                        levels: Optional[List[Dict]] = None) -> Dict[str, Any]:
        return self.call("prepare_project",
                         {"template": template, "units_mm": units_mm,
                          "levels": levels or []})

    def create_levels(self, levels: List[Dict]) -> Dict[str, Any]:
        return self.call("create_levels", {"levels": levels})

    def create_grids(self, grids: List[Dict]) -> Dict[str, Any]:
        return self.call("create_grids", {"grids": grids})

    def create_walls(self, walls: List[Dict], mode: str = "batch") -> Dict[str, Any]:
        return self.call("create_walls", {"walls": walls, "mode": mode})

    def create_columns(self, columns: List[Dict]) -> Dict[str, Any]:
        return self.call("create_columns", {"columns": columns})

    def create_beams(self, beams: List[Dict]) -> Dict[str, Any]:
        return self.call("create_beams", {"beams": beams})

    def create_openings(self, doors: List[Dict], windows: List[Dict]) -> Dict[str, Any]:
        return self.call("create_openings", {"doors": doors, "windows": windows})

    def create_slabs(self, slabs: List[Dict]) -> Dict[str, Any]:
        return self.call("create_slabs", {"slabs": slabs})

    def delete_tagged(self) -> Dict[str, Any]:
        """删除上一次翻模生成的构件（按 Comments 的 revit-wy: 标记，不动手工构件）。"""
        return self.call("delete_tagged", {})

    def select_by_tags(self, ids: List[str],
                       kinds: Optional[List[str]] = None) -> Dict[str, Any]:
        """按 IR 构件 id 在 Revit 里选中并缩放（浏览器工作台联动用）。"""
        return self.call("select_by_tags", {"ids": ids, "kinds": kinds or []},
                         timeout=60)

    def build_ir(self, ir: Dict[str, Any], steps: Optional[List[str]] = None,
                 dry_run: bool = False, auto_save: bool = True,
                 timeout: Optional[float] = None) -> Dict[str, Any]:
        """一次性下发整份 IR，由插件端按序执行（推荐：一次事务、可回滚）。"""
        return self.call("build_ir", {
            "ir": ir,
            "steps": steps or ["levels", "grids", "columns", "walls",
                               "beams", "openings", "slabs"],
            "dry_run": dry_run,
            "auto_save": auto_save,
        }, timeout=timeout or 1800)


def probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          timeout: float = 3.0) -> Dict[str, Any]:
    """探活，用于 CLI / MCP 的 doctor。"""
    b = RevitBridge(host, port, timeout=timeout)
    try:
        b.connect(retries=1)
        info = b.ping()
        return {"connected": True, **info}
    except Exception as ex:
        return {"connected": False, "error": str(ex), "host": host, "port": port}
    finally:
        b.close()
