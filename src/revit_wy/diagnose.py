"""环境诊断：把"为什么跑不起来"变成一份能直接发出去的文本报告。

为什么用 Python 而不是 .bat
---------------------------
上一版 `一键诊断.bat` 全靠 batch 写，踩了两个真实的坑：
  · `>> file echo` 与 `if (...)` 括号块混用会解析错位，输出被截断成
    `'EPORT" echo [X] 未部署插件' is not recognized`；
  · batch 没有能力检查"MCP 配置里的 python 路径是否真的存在"这类语义问题。

所以 .bat 只负责找到 Python 然后调用本模块，所有判断逻辑在 Python 里，
可测试、可读、可在 Linux 上验证。

输出分两种：
  · 人看的：分段打印，[OK]/[!]/[X] 一眼看出哪里断了
  · 报告文件：完整信息 + 修复建议，直接发给支持人员

设计原则：**每条失败都必须带一条具体建议**。只说"未找到 Revit"没有价值，
要说"没有 Revit 也能用到哪一步、要去装什么"。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 支持 Revit 2022 及以上。版本与运行时（Autodesk 官方口径）：
#   2022-2024 -> .NET Framework 4.8
#   2025-2026 -> .NET 8
#   2027+     -> .NET 10
# 扫描按年递增，遇到 2035 停（防御未来目录名异常）。
SUPPORTED_MIN = 2022
REVIT_VERSIONS = [str(y) for y in range(2022, 2036)]
DEFAULT_PORT = 48880
VIEWER_PORT = 8770


@dataclass
class Item:
    """一条检查结果。level: ok / warn / bad / info"""
    title: str
    level: str = "info"
    detail: str = ""
    fix: str = ""

    def line(self) -> str:
        mark = {"ok": "[OK]", "warn": "[! ]", "bad": "[X ]", "info": "[ ] "}[self.level]
        s = f"  {mark} {self.title}"
        if self.detail:
            s += f"  {self.detail}"
        return s


@dataclass
class Section:
    name: str
    items: List[Item] = field(default_factory=list)

    def add(self, title: str, level: str = "info", detail: str = ".",
            fix: str = "") -> Item:
        it = Item(title=title, level=level, detail="" if detail == "." else detail,
                  fix=fix)
        self.items.append(it)
        return it


# ------------------------------------------------------------------ 各段检查
def _clean(raw: str | Path) -> Path:
    """与 install_mcp.clean_path 同一套清洗规则。

    Windows 上 `--root "D:\\path\\"` 的尾反斜杠会转义闭合引号，
    导致整个路径变成 `D:\\path" --python ...`。这里必须能自愈，
    否则诊断报告会把一个明显的引号问题报成"虚拟环境不存在"。
    """
    s = str(raw).strip().strip('"').strip("'").rstrip("\\/").strip()
    if '"' in s:
        s = s.split('"')[0].rstrip("\\/")
    return Path(s)


def _venv_python(root: Path) -> Path:
    """找 .venv 里的 python。

    兼容两种情形：从仓库根运行，以及直接用 .venv 里的解释器运行（此时
    root 可能指错）。后者很常见——用户双击的那个 .bat 用的就是 .venv 的 python。
    """
    cands = [root / ".venv" / ("Scripts/python.exe" if os.name == "nt"
                               else "bin/python")]
    exe = Path(sys.executable)
    if ".venv" in exe.parts:
        cands.append(exe)
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def check_python(sec: Section, root: Path) -> None:
    vpy = _venv_python(root)
    sec.add("Python（当前进程）", "ok", f"{platform.python_version()} @ {sys.executable}")
    if root.name and " " in root.name or '"' in str(root):
        sec.add("仓库路径可疑", "warn", f"{root}",
                "路径含引号/异常字符，建议换到不含空格与特殊符号的目录")
    if vpy.exists():
        sec.add("虚拟环境 .venv", "ok", str(vpy))
        try:
            out = subprocess.run([str(vpy), "-c",
                                  "import ezdxf,yaml;print(ezdxf.__version__)"],
                                 capture_output=True, text=True, timeout=40)
            if out.returncode == 0:
                sec.add("依赖 ezdxf / pyyaml", "ok", f"ezdxf {out.stdout.strip()}")
            else:
                sec.add("依赖缺失", "bad", (out.stderr or "").strip()[:200],
                        "双击「一键启动.bat」，或执行 .venv\\Scripts\\pip install -e .")
        except Exception as ex:
            sec.add("依赖检查失败", "warn", str(ex)[:160])
    else:
        sec.add("虚拟环境 .venv", "bad", "不存在",
                "双击「一键启动.bat」自动创建；或手动 python -m venv .venv")

    try:
        import revit_wy
        sec.add("revit_wy 可导入", "ok", f"v{revit_wy.__version__} @ {Path(revit_wy.__file__).parent}")
    except Exception as ex:
        sec.add("revit_wy 无法导入", "bad", str(ex)[:200],
                "执行 pip install -e . 或设置 PYTHONPATH 到 src 目录")


def _revit_dirs() -> List[Path]:
    base = Path(r"C:\Program Files\Autodesk")
    out = []
    if base.exists():
        for v in REVIT_VERSIONS:
            d = base / f"Revit {v}"
            if (d / "Revit.exe").exists():
                out.append(d)
    return out


def dotnet_framework_for(version: str) -> str:
    """Revit 版本 → 该版本插件必须编译到的运行时。用错的表现是静默不加载。"""
    v = int(version)
    if v >= 2027:
        return "net10.0-windows"
    if v >= 2025:
        return "net8.0-windows"
    return "net48"


def check_revit(sec: Section) -> None:
    found = _revit_dirs()
    if not found:
        sec.add("Revit", "warn", f"本机未安装 Revit {SUPPORTED_MIN} 及以上",
                f"没有 Revit 也能用：图纸清洗→数模对照→构件清单→可视化预览→导出 "
                f"全部可用，只是不能真正写入模型。要写入需安装 Revit {SUPPORTED_MIN} 或更高。")
        return
    versions = sorted(d.name.replace("Revit ", "") for d in found)
    for d, ver in zip(found, versions):
        sec.add(f"Revit {ver}", "ok",
                f"{d}（插件需编译到 {dotnet_framework_for(ver)}）")
    sec.add("检测到版本", "ok", "、".join(versions) + "（项目支持 2022 及以上，含这些版本）")


def check_addin(sec: Section) -> None:
    appdata = Path(os.environ.get("APPDATA", ""))
    added = False
    installed = {d.name.replace("Revit ", "") for d in _revit_dirs()}
    for v in REVIT_VERSIONS:
        d = appdata / "Autodesk" / "Revit" / "Addins" / v
        manifest = d / "RevitWy.Plugin.addin"
        if manifest.exists():
            added = True
            dll = d / "revit-wy" / "RevitWy.Plugin.dll"
            sec.add(f"插件清单（{v}）", "ok", str(manifest))
            if dll.exists():
                sec.add(f"插件 DLL（{v}）", "ok",
                        f"{dll.stat().st_size // 1024} KB")
            else:
                sec.add(f"插件 DLL（{v}）", "bad", "清单存在但 DLL 缺失",
                        "重跑 scripts\\build-addin.ps1 -Deploy（清单与 DLL 必须成对）")
    if not added:
        sec.add("Revit 插件", "warn", "未部署 .addin 清单",
                "执行 scripts\\build-addin.ps1 -Deploy（会自动按已装版本逐个编译部署）；"
                f"无 Revit 的机器跳过此项即可")
    elif installed:
        deployed = {it.title.split("（")[1].rstrip("）")
                    for it in sec.items
                    if it.level == "ok" and it.title.startswith("插件清单")}
        missing = sorted(installed - deployed)
        if missing:
            sec.add("插件未覆盖的 Revit 版本", "warn", "、".join(missing),
                    "这些版本装了但没部署插件，Revit 里不会有 revit-wy 页签；"
                    "运行 scripts\\build-addin.ps1 -Deploy 会自动补齐")


def _port_open(port: int, timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def check_ports(sec: Section, port: int = DEFAULT_PORT,
                viewer_port: int = VIEWER_PORT) -> None:
    if _port_open(port):
        sec.add(f"Revit 插件端口 {port}", "ok", "连通（插件在监听）")
    else:
        sec.add(f"Revit 插件端口 {port}", "warn", "未连通",
                "① Revit 是否已打开目标项目；② 功能区是否出现 revit-wy 页签"
                "（没出现=插件未加载）；③ 端口是否与 MCP 配置一致")
    if _port_open(viewer_port):
        sec.add(f"工作台端口 {viewer_port}", "ok", "运行中 → http://127.0.0.1:%d/" % viewer_port)
    else:
        sec.add(f"工作台端口 {viewer_port}", "info", "未运行",
                "需要时执行 revit-wy panel <图纸.dxf>")


def _load_json(p: Path) -> tuple[Optional[Dict[str, Any]], str]:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")), ""
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"


def check_mcp(sec: Section, root: Path) -> None:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    targets = [
        ("Cursor（全局）", home / ".cursor" / "mcp.json"),
        ("Cursor（项目）", root / ".cursor" / "mcp.json"),
        ("WorkBuddy", home / ".workbuddy" / "mcp.json"),
    ]
    for label, path in targets:
        if not path.exists():
            sec.add(label, "info", f"未配置（{path}）",
                    "双击「一键启动.bat」会自动写入；或参考 clients/ 下的样例手工合并")
            continue
        data, err = _load_json(path)
        if err:
            sec.add(label, "bad", f"JSON 语法错误：{err}",
                    f"用编辑器打开 {path} 检查括号/逗号/引号；尾逗号是最常见原因")
            continue
        servers = (data or {}).get("mcpServers") or {}
        if "revit-wy" not in servers:
            sec.add(label, "warn", f"存在但没有 revit-wy 条目（现有 {len(servers)} 个）",
                    f"把 clients/ 里的样例合并进 {path} 的 mcpServers")
            continue
        entry = servers["revit-wy"]
        cmd = str(entry.get("command", ""))
        sec.add(label, "ok", f"已配置 command={cmd}")
        # 关键：command 写的路径是否真的存在（这是最常见的"配了但不生效"）
        if cmd and (os.path.isabs(cmd) or ":" in cmd or "/" in cmd):
            if Path(cmd).exists():
                sec.add(f"{label} 的 python 路径", "ok", cmd)
            else:
                sec.add(f"{label} 的 python 路径", "bad", f"不存在：{cmd}",
                        "改成 .venv\\Scripts\\python.exe 的绝对路径，或直接改回 python")
        elif shutil.which(cmd) is None and cmd:
            sec.add(f"{label} 的 command", "warn", f"PATH 里找不到 {cmd}",
                    "建议写绝对路径，避免客户端启动时 PATH 不同")
        env = entry.get("env") or {}
        if env.get("PYTHONPATH"):
            sec.add(f"{label} 的 PYTHONPATH", "ok", str(env["PYTHONPATH"]))
        else:
            sec.add(f"{label} 的 PYTHONPATH", "warn", "未设置",
                    "若没用 pip install -e .，必须设 PYTHONPATH 指向 src 目录")


def check_logs(sec: Section) -> None:
    appdata = Path(os.environ.get("APPDATA", ""))
    cands = [appdata / "revit-wy" / "plugin.log",
             Path.home() / ".revit-wy" / "plugin.log"]
    hit = next((p for p in cands if p.exists()), None)
    if not hit:
        sec.add("插件日志", "info", "无 plugin.log（插件从未启动过）",
                "启动 Revit 后日志才会生成；若 Revit 已开仍无日志，说明插件没被加载")
        return
    try:
        lines = hit.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
        errs = [l for l in lines if "ERROR" in l.upper() or "Exception" in l]
        sec.add("插件日志", "ok" if not errs else "warn",
                f"{hit}（末 {len(lines)} 行，其中 {len(errs)} 行报错）")
        sec.extra_log = lines if hasattr(sec, "extra_log") else lines
    except Exception as ex:
        sec.add("插件日志读取失败", "warn", str(ex)[:120])


def check_pipeline(sec: Section, root: Path) -> None:
    """真跑一遍五阶段 —— 能跑通才算环境可用，而不是只看包有没有装。"""
    sample = root / "samples" / "arch_plan.dxf"
    if not sample.exists():
        sec.add("流水线自检", "warn", "缺少样例图 samples/arch_plan.dxf",
                "执行 tests 里的样例生成，或跳过（不影响真实图纸）")
        return
    try:
        from revit_wy.dwg_pipeline import run_dwg
        r = run_dwg(sample, "arch", Path(os.environ.get("TEMP", "/tmp")) / "wy-diag")
        if r.ok:
            sec.add("流水线自检", "ok",
                    f"五阶段全通，生成 {sum(r.counts.values())} 个构件")
        else:
            bad = next((s.name for s in r.stages if not s.ok), "?")
            sec.add("流水线自检", "bad", f"卡在 {bad}",
                    "；".join(r.blockers[:2]) or "看阶段明细")
    except Exception as ex:
        sec.add("流水线自检", "bad", f"{type(ex).__name__}: {ex}",
                "通常是依赖缺失或配置被改坏；重跑 一键启动.bat")


# ------------------------------------------------------------------ 汇总
def run_all(root: str | Path = ".", port: int = DEFAULT_PORT,
            viewer_port: int = VIEWER_PORT) -> List[Section]:
    root = _clean(root).resolve()
    secs = [
        Section("1. Python 与依赖"),
        Section("2. Revit 与本项目插件"),
        Section("3. 端口连通性"),
        Section("4. MCP 客户端配置"),
        Section("5. 流水线自检"),
        Section("6. 日志"),
    ]
    check_python(secs[0], root)
    check_revit(secs[1])
    check_addin(secs[1])
    check_ports(secs[2], port, viewer_port)
    check_mcp(secs[3], root)
    check_pipeline(secs[4], root)
    check_logs(secs[5])
    return secs


def summarize(secs: List[Section]) -> Dict[str, int]:
    c = {"ok": 0, "warn": 0, "bad": 0, "info": 0}
    for s in secs:
        for it in s.items:
            c[it.level] = c.get(it.level, 0) + 1
    return c


def verdict(secs: List[Section]) -> tuple[str, List[str]]:
    """给出"当前能走到哪一步"的判断 + 待办清单。"""
    todos = [f"{it.title}：{it.fix}" for s in secs for it in s.items
             if it.level in ("bad", "warn") and it.fix]
    has_revit = any(it.level == "ok" and it.title.startswith("Revit 20")
                    for s in secs for it in s.items)
    pipeline_ok = any(it.title == "流水线自检" and it.level == "ok"
                      for s in secs for it in s.items)
    if pipeline_ok and has_revit:
        return "A. 全功能可用（含写入 Revit）", todos
    if pipeline_ok:
        return ("B. 可用到「构件清单/可视化/导出」，不能写入 Revit 模型", todos)
    return "C. 环境未跑通，先按下面的建议逐条处理", todos


# Windows 控制台默认 GBK，em dash / 箭头 / 圈号会显示成乱码。
# 报告文件用 UTF-8-BOM（记事本正常），但**打印到屏幕**的副本要降级成 ASCII 安全字符。
_SAFE_MAP = {
    "→": "->", "←": "<-", "✅": "[OK]", "❌": "[X]", "⚠": "[!]",
    "①②③④⑤": "1)2)3)4)5)", "①": "1)", "②": "2)", "③": "3)",
    "④": "4)", "⑤": "5)", "✔": "[v]", "✘": "[x]", "—": "-",
    "「": "[", "」": "]", "：": ": ", "（": "(", "）": ")",
}


def console_safe(text: str) -> str:
    for k, v in _SAFE_MAP.items():
        text = text.replace(k, v)
    return text


def render(secs: List[Section], meta: Optional[Dict[str, Any]] = None) -> str:
    meta = meta or {}
    c = summarize(secs)
    L = ["===== revit-wy 诊断报告 =====",
         f"生成时间: {meta.get('time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}",
         f"计算机名: {meta.get('host', platform.node())}",
         f"用户:     {meta.get('user', os.environ.get('USERNAME', ''))}",
         f"平台:     {platform.platform()}",
         ""]
    for s in secs:
        L.append(f"----- {s.name} -----")
        for it in s.items:
            L.append(it.line())
            if it.fix and it.level in ("bad", "warn"):
                L.append(f"          → {it.fix}")
        L.append("")
    head, todos = verdict(secs)
    L += ["===== 结论 =====", f"当前状态：{head}", ""]
    if todos:
        L.append("待处理（按影响排序）：")
        L += [f"  {i}. {t}" for i, t in enumerate(todos, 1)]
    else:
        L.append("未发现需要处理的问题。")
    L += ["", f"统计：OK {c['ok']} / 警告 {c['warn']} / 失败 {c['bad']}"]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser("revit-wy 诊断")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--viewer-port", type=int, default=VIEWER_PORT)
    ap.add_argument("--quiet", action="store_true", help="只写文件不打印")
    a = ap.parse_args(argv)

    secs = run_all(a.root, a.port, a.viewer_port)
    text = render(secs)
    if not a.quiet:
        # 屏幕副本降级到 ASCII 安全字符；文件仍保留完整符号
        try:
            print(console_safe(text))
        except UnicodeEncodeError:
            sys.stdout.buffer.write(
                console_safe(text).encode(sys.stdout.encoding or "utf-8", "replace"))
    out = Path(a.out) if a.out else Path(
        f"诊断报告_{platform.node()}_{datetime.now():%Y%m%d_%H%M%S}.txt")
    out.write_text(text, encoding="utf-8-sig")   # 带 BOM，记事本打开不乱码
    print(f"\n报告文件：{out}")
    c = summarize(secs)
    return 0 if c["bad"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
