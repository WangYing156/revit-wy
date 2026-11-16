"""把 MCP 客户端配置写好，且**不破坏用户已有的配置**。

为什么必须用 Python 做
----------------------
`一键启动.bat` 原来用 PowerShell 拼一段 JSON 字符串写文件，有三个问题：
  · 用户已存在的 mcp.json 会被整体覆盖（把别人配的服务器全冲掉）；
  · 路径里的反斜杠要转义四层，出一次错就很难看；
  · 无法校验"写完的 JSON 是否合法、command 指向的 python 是否真的存在"。

所以这里做三件事：**读→合并→写**，并校验结果。幂等：重复执行不会产生重复条目。

写入位置
--------
  · 项目级 `<repo>/.cursor/mcp.json`  —— Cursor 推荐项目级，配置随项目走
  · 用户级 `%USERPROFILE%\\.cursor\\mcp.json` —— 可选（--cursor-global）
  · `%USERPROFILE%\\.workbuddy\\mcp.json` —— WorkBuddy（需在客户端点 Trust）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_NAME = "revit-wy"


def clean_path(raw: str) -> Path:
    """把命令行传进来的路径洗干净。

    真实事故：Windows 上 `--root "F:\\...\\revit-wy\\"` 结尾的反斜杠把闭合引号
    转义掉了，cmd 于是把后面的 `--python "..."` 一起塞进了 --root 的值里。
    结果 PYTHONPATH 变成 `...\\revit-wy" --python ...\\python.exe\\src`。
    .bat 已改成不带尾反斜杠，但入口这里也必须自己兜住——参数脏了要能看出来，
    而不是默默写出一个坏配置。
    """
    s = str(raw).strip().strip('"').strip("'").rstrip("\\/").strip()
    # 上面只去掉尾部，这里再防一手：路径中间出现引号说明已经被拼坏了
    if '"' in s:
        s = s.split('"')[0].rstrip("\\/")
    return Path(s)


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def build_entry(root: Path, profile: str = "arch",
                python: str = "") -> Dict[str, Any]:
    py = python or sys.executable
    return {
        "type": "stdio",
        "command": py,
        "args": ["-m", "revit_wy.mcp_server", "--profile", profile],
        "env": {
            # 即使没 pip install -e .，靠 PYTHONPATH 也能跑
            "PYTHONPATH": str(root / "src"),
            "REVIT_WY_HOME": str(_home() / ".revit-wy"),
        },
    }


def _read(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # 已有文件坏了：备份后再重建，绝不静默丢弃用户配置
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        print(f"  [!] {path} 不是合法 JSON，已备份到 {bak.name} 后重建")
        return {}


def merge_into(path: Path, entry: Dict[str, Any],
               force: bool = False) -> str:
    """把 entry 合并进 path 的 mcpServers[revit-wy]。返回动作描述。"""
    data = _read(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = data["mcpServers"] = {}
    existed = SERVER_NAME in servers
    if existed and not force and servers[SERVER_NAME] == entry:
        return "已是最新，无需改动"
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    # 写完立刻校验，坏文件不能留给客户端
    try:
        json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as ex:
        raise RuntimeError(f"写入后 JSON 校验失败（{ex}），请检查 {path}")
    return ("已更新" if existed else "已新增") + f"（保留原有 {len(servers) - 1} 个服务器）"


def install(root: str | Path = ".", profile: str = "arch",
            python: str = "", force: bool = False,
            cursor_global: bool = False,
            workbuddy: bool = True,
            cursor_project: bool = True) -> List[Dict[str, str]]:
    root = clean_path(root).resolve()
    python = str(clean_path(python)) if python else ""
    entry = build_entry(root, profile, python)
    results: List[Dict[str, str]] = []

    targets = []
    if cursor_project:
        targets.append(("Cursor（项目）", root / ".cursor" / "mcp.json"))
    if cursor_global:
        targets.append(("Cursor（全局）", _home() / ".cursor" / "mcp.json"))
    if workbuddy:
        targets.append(("WorkBuddy", _home() / ".workbuddy" / "mcp.json"))

    for label, path in targets:
        try:
            action = merge_into(path, entry, force=force)
            results.append({"client": label, "path": str(path),
                            "ok": "true", "action": action})
        except Exception as ex:
            results.append({"client": label, "path": str(path),
                            "ok": "false", "action": f"失败：{ex}"})
    return results


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser("revit-wy MCP 配置安装")
    ap.add_argument("--root", default=".")
    ap.add_argument("--profile", default="arch",
                    choices=["arch", "struct", "mep"])
    ap.add_argument("--python", default="", help="客户端要用的 python 绝对路径")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cursor-global", action="store_true")
    ap.add_argument("--no-workbuddy", action="store_true")
    a = ap.parse_args(argv)

    rows = install(a.root, a.profile, a.python, a.force,
                   a.cursor_global, not a.no_workbuddy)
    bad = 0
    for r in rows:
        mark = "OK " if r["ok"] == "true" else "X  "
        print(f"  [{mark}] {r['client']:<12} {r['action']}")
        print(f"         {r['path']}")
        if r["ok"] != "true":
            bad += 1
    if bad == 0:
        print("\n  提示：WorkBuddy 需要在「连接器」里点一次 Trust，工具才会出现。")
        print("        Cursor 首次会询问是否启用该项目级的 MCP 服务器。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
