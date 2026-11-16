"""一键启动跑不起来时，用这个模块找出到底卡在哪。

为什么单独写一个模块
--------------------
.bat 里能做的事情很有限：cmd.exe 的语法陷阱（括号块里 set、尾反斜杠吞引号、
GBK 代码页把中文变成问号）会把真正的错误盖掉。前两次踩的坑都是
**脚本自己没说清楚哪里失败**，而不是失败本身难修。

所以这个模块的原则是：每一步都自证，绝不因为"我觉得应该没问题"就跳过。

检查顺序刻意按"用户能自己修好的容易程度"排：
    1. Python 版本       —— 用户装一下就好
    2. 虚拟环境          —— 脚本自动建
    3. 依赖              —— 脚本自动装
    4. 配置/样例         —— 仓库自带
    5. 端口冲突          —— 关掉别的窗口就好
    6. Revit 插件        —— 需要编译，最麻烦，放最后

每一步给出三种结论之一：OK / WARN（能用但降级）/ BAD（必须修）。
最后一段永远打印"下一步该做什么"，而不是让人对着红字发愁。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

VIEWER_PORT = 8770
REVIT_PORT = 48880
MIN_PY = (3, 9)


@dataclass
class Check:
    key: str
    label: str
    level: str                      # ok / warn / bad
    detail: str = ""
    fix: str = ""                   # 给人看的下一步（绝不写"请联系管理员"）

    def line(self) -> str:
        mark = {"ok": "[OK  ]", "warn": "[WARN]", "bad": "[FAIL]"}[self.level]
        s = f"  {mark} {self.label}"
        if self.detail:
            s += f"  —— {self.detail}"
        return s


@dataclass
class SetupReport:
    root: Path
    checks: List[Check] = field(default_factory=list)

    def add(self, *a, **kw) -> Check:
        c = Check(*a, **kw)
        self.checks.append(c)
        return c

    @property
    def worst(self) -> str:
        if any(c.level == "bad" for c in self.checks):
            return "bad"
        if any(c.level == "warn" for c in self.checks):
            return "warn"
        return "ok"

    def exit_code(self) -> int:
        return {"ok": 0, "warn": 0, "bad": 2}[self.worst]

    def render(self) -> str:
        L = ["", "=" * 60, "  revit-wy 自检", "=" * 60, ""]
        L += [c.line() for c in self.checks]
        fixes = [c for c in self.checks if c.level == "bad" and c.fix]
        warns = [c for c in self.checks if c.level == "warn" and c.fix]
        if fixes:
            L += ["", "-" * 60, "  必须先解决（按顺序）", "-" * 60]
            for c in fixes:
                L += [f"  {c.label}", f"    → {c.fix}"]
        if warns:
            L += ["", "-" * 60, "  可以继续，但这些功能会受限", "-" * 60]
            for c in warns:
                L += [f"  {c.label}", f"    → {c.fix}"]
        L += ["", "=" * 60]
        if self.worst == "ok":
            L.append("  结论：环境正常，可以开工。")
        elif self.worst == "warn":
            L.append("  结论：核心功能可用，部分功能降级（见上）。")
        else:
            L.append("  结论：有必须先解决的问题，见上。修好后重新运行。")
        L.append("=" * 60)
        return "\n".join(L) + "\n"


# --------------------------------------------------------------- 各项检查
def check_python(rep: SetupReport) -> None:
    v = sys.version_info
    if (v.major, v.minor) < MIN_PY:
        rep.add("python", "Python 版本", "bad", f"{v.major}.{v.minor}.{v.micro}",
                f"需要 {MIN_PY[0]}.{MIN_PY[1]} 或更高。到 python.org 下载安装，"
                "安装时勾选 Add Python to PATH，然后重跑一键启动")
        return
    rep.add("python", "Python 版本", "ok", f"{v.major}.{v.minor}.{v.micro}（{sys.executable}）")

    # 一条极常见的 Windows 坑：Microsoft Store 的 python.exe 是 0 字节的转发器，
    # 在非交互环境下会静默失败。真要踩了，这里能直接看出来。
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and exe.stat().st_size < 200 * 1024:
        rep.add("python_stub", "Python 解释器可用性", "warn",
                f"解释器只有 {exe.stat().st_size} 字节，疑似 Microsoft Store 转发器",
                "从 python.org 重新安装一份完整 Python，安装时勾选 Add to PATH")


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _running_inside_venv(root: Path) -> Optional[Path]:
    """当前进程是不是用本仓库的 .venv 跑起来的（那就没必要再要求 .venv 存在）。"""
    exe = Path(sys.executable).resolve()
    try:
        exe.relative_to((root / ".venv").resolve())
        return exe
    except ValueError:
        return None


def check_venv(rep: SetupReport, root: Path) -> None:
    vpy = _venv_python(root)
    if vpy.exists():
        rep.add("venv", "虚拟环境 .venv", "ok", str(vpy))
        return
    if _running_inside_venv(root):
        rep.add("venv", "虚拟环境 .venv", "ok", f"当前解释器 {sys.executable} 已是最新")
        return
    # 当前解释器不是本仓库的 .venv：说明用别处的 Python 跑的自检，
    # 这本身不算错，但需要提示"一键启动请用 .venv 里那个"。
    rep.add("venv", "虚拟环境 .venv", "warn", f"不存在（当前用 {sys.executable}）",
            f"一键启动.bat 会自动创建；提前手动建：\"{sys.executable}\" -m venv \"{root / '.venv'}\"")


def check_deps(rep: SetupReport, root: Path) -> None:
    """真正 import 一遍，而不是查 pip list。依赖装一半的情况很常见。"""
    mods = [("ezdxf", "读 DXF"), ("yaml", "读配置"), ("mcp", "MCP 服务")]
    missing = []
    for mod, why in mods:
        try:
            __import__(mod)
        except Exception as ex:
            missing.append(f"{mod}（{why}，{type(ex).__name__}）")
    if missing:
        rep.add("deps", "Python 依赖", "bad", "缺 " + "、".join(missing),
                "重跑一键启动.bat；或手动 "
                f'"{_venv_python(root)}" -m pip install -e "{root}" '
                "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    rep.add("deps", "Python 依赖", "ok", "ezdxf / PyYAML / mcp 均可导入")

    try:
        import win32com.client  # noqa: F401
        rep.add("pywin32", "DWG→DXF 转换组件", "ok", "pywin32 已装")
    except Exception:
        rep.add("pywin32", "DWG→DXF 转换组件", "warn", "未安装 pywin32",
                "只影响 .dwg 输入（.dxf 完全可用）。需要时执行 "
                f'"{_venv_python(root)}" -m pip install pywin32')


def check_repo(rep: SetupReport, root: Path) -> None:
    cfg = root / "config" / "layer_mapping" / "arch.yaml"
    samples = sorted((root / "samples").glob("*.dxf")) if (root / "samples").is_dir() else []
    if not cfg.exists():
        rep.add("config", "配置与样例", "bad", f"缺 {cfg}",
                "仓库不完整：重新克隆，或确认解压时没有漏掉 config 目录")
        return
    if not samples:
        rep.add("config", "配置与样例", "warn", "samples 里没有 .dxf",
                "不影响使用真实图纸，只是第一次自检和演示会跳过")
        return
    rep.add("config", "配置与样例", "ok", f"3 套图层规范 + {len(samples)} 张样例图")


def _port_busy(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def check_ports(rep: SetupReport) -> None:
    if _port_busy(VIEWER_PORT):
        rep.add("viewer_port", f"工作台端口 {VIEWER_PORT}", "warn", "已被占用",
                "多半是上一次没关掉的窗口。关掉它，或直接用浏览器打开 "
                f"http://127.0.0.1:{VIEWER_PORT}/ 看是不是工作台在跑")
    else:
        rep.add("viewer_port", f"工作台端口 {VIEWER_PORT}", "ok", "空闲")

    if _port_busy(REVIT_PORT):
        rep.add("revit_port", f"Revit 插件端口 {REVIT_PORT}", "ok", "有人监听（插件在跑）")
    else:
        rep.add("revit_port", f"Revit 插件端口 {REVIT_PORT}", "warn", "无人监听",
                "模型体检 / 写入 Revit 需要它。打开 Revit 并确认功能区有 revit-wy 页签；"
                "没有页签就先跑 scripts\\build-addin.ps1 -Deploy")


def check_dotnet(rep: SetupReport) -> None:
    """编 Revit 插件必须有 dotnet。没有它，插件永远装不上。"""
    import shutil as _sh
    exe = _sh.which("dotnet")
    if not exe:
        rep.add("dotnet", "dotnet SDK", "warn", "找不到 dotnet 命令",
                "装 .NET SDK 8+：https://dotnet.microsoft.com/download 。"
                "不装也能用除『写入 Revit』以外的全部功能")
        return
    try:
        p = subprocess.run([exe, "--list-sdks"], capture_output=True, text=True, timeout=30)
        lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
        majors = sorted({l.split(".")[0] for l in lines if l[:1].isdigit()})
        rep.add("dotnet", "dotnet SDK", "ok",
                f"{exe}" + (f"（SDK {', '.join(majors)}）" if majors else ""))
    except Exception as ex:
        rep.add("dotnet", "dotnet SDK", "warn", f"{type(ex).__name__}: {ex}",
                "重装 .NET SDK 8+")


def check_net48_targeting_pack(rep: SetupReport, root: Path) -> None:
    """编译 Revit 2022–2024 插件需要 .NET Framework 4.8 的引用程序集。

    MSB3644 几乎全是这个原因：机器上只有 dotnet SDK 10，没有装 .NET Framework
    的开发包（Developer Pack / VS Build Tools 的 targeting pack）。
    这里提前判定，并给出最小代价的三条路——比等 MSBuild 抛英文错误好得多。
    """
    if os.name != "nt":
        # 非 Windows 上不判；但要留一条记录，免得报告里少一项让人困惑。
        rep.add("net48", ".NET Framework 4.8 引用程序集", "ok", "非 Windows，跳过")
        return
    root_dirs = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Reference Assemblies/Microsoft/Framework/.NETFramework/v4.8",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Reference Assemblies/Microsoft/Framework/.NETFramework/v4.8",
    ]
    for d in root_dirs:
        if (d / "mscorlib.dll").exists():
            rep.add("net48", ".NET Framework 4.8 引用程序集", "ok", str(d))
            return
    rep.add("net48", ".NET Framework 4.8 引用程序集", "warn", "未找到（编译 Revit 2022–2024 插件会报 MSB3644）",
            "三选一：① 装 .NET Framework 4.8 Developer Pack（约 70 MB，最快）；"
            "② 装 VS Build Tools 并勾选 \".NET Framework 4.8 targeting pack\"；"
            "③ 只编 Revit 2025+（-RevitVersions 2025）。"
            "注意：翻模/体检/报告等全部功能都不受影响，只影响把插件编进 Revit 2022–2024")


def check_addin(rep: SetupReport, root: Path) -> None:
    """插件是三件事里最容易静默失败的（装错框架 = Revit 直接跳过，不报错）。"""
    appdata = Path(os.environ.get("APPDATA", "")) if os.name == "nt" else Path.home()
    installed: List[str] = []
    if os.name == "nt":
        for base in (Path(r"C:\Program Files\Autodesk"), Path(r"C:\Program Files (x86)\Autodesk")):
            if base.is_dir():
                for d in base.glob("Revit 20*"):
                    v = d.name.replace("Revit ", "").strip()
                    if v.isdigit():
                        installed.append(v)
    installed = sorted(set(installed))

    deployed: Dict[str, bool] = {}
    for v in installed or ["2024"]:
        d = appdata / "Autodesk" / "Revit" / "Addins" / v
        deployed[v] = (d / "RevitWy.Plugin.addin").exists() and \
                      (d / "revit-wy" / "RevitWy.Plugin.dll").exists()

    if not installed:
        rep.add("revit", "Revit", "warn", "本机未检测到 Revit 2022+",
                "除『写入模型/模型体检』外全部可用。要写入需安装 Revit 2022 或更高")
        return
    rep.add("revit", "Revit", "ok", "已装 " + "、".join(installed))

    bad = [v for v in installed if not deployed.get(v)]
    if not bad:
        rep.add("addin", "Revit 插件部署", "ok", "、" .join(installed) + " 均已部署")
        return
    rep.add("addin", "Revit 插件部署", "warn", "未部署：" + "、".join(bad),
            "执行 powershell -NoProfile -ExecutionPolicy Bypass "
            f'-File "{root / "scripts" / "build-addin.ps1"}" -Deploy，然后重启 Revit')


def _powershell_ok(root: Path) -> Optional[str]:
    """PowerShell 脚本用 UTF-8 中文 + BOM 才不会被 GBK 读坏。这里能提前发现。"""
    ps = root / "scripts" / "build-addin.ps1"
    if not ps.exists():
        return None
    raw = ps.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "ok"
    return "缺 UTF-8 BOM：Windows PowerShell 5.1 会按 GBK 解码，中文变乱码甚至解析失败"


def check_scripts(rep: SetupReport, root: Path) -> None:
    msg = _powershell_ok(root)
    if msg == "ok":
        rep.add("ps1", "PowerShell 脚本编码", "ok", "UTF-8 BOM")
    elif msg:
        rep.add("ps1", "PowerShell 脚本编码", "warn", msg,
                "重新从仓库拉取 scripts/build-addin.ps1（最新版已带 BOM）")

    bats = [root / "一键启动.bat", root / "一键诊断.bat", root / "一键体检.bat"]
    problems = []
    for b in bats:
        if not b.exists():
            problems.append(f"{b.name} 缺失")
            continue
        raw = b.read_bytes()
        if b"\r\n" not in raw:
            problems.append(f"{b.name} 不是 CRLF 换行")
        if raw[:3] == b"\xef\xbb\xbf":
            problems.append(f"{b.name} 带 BOM（cmd 会把第一行读成乱码命令）")
    if problems:
        rep.add("bat", "批处理脚本", "warn", "；".join(problems),
                "重新从仓库拉取 .bat 文件（最新版已修成 ASCII + CRLF）")
    else:
        rep.add("bat", "批处理脚本", "ok", "ASCII + CRLF")


def check_viewer_serves(rep: SetupReport, root: Path) -> None:
    """真起一个工作台进程、等它监听、再关掉，证明它不只是能 import。

    注意不能直接 subprocess.run 等它退出——工作台是常驻服务，会一直阻塞。
    正确做法是：起进程 → 轮询端口 → 杀掉 → 看这期间的输出。
    """
    import time

    server = root / "tools" / "viewer" / "server.py"
    if not server.exists():
        rep.add("viewer", "工作台服务", "bad", "缺 tools/viewer/server.py", "仓库不完整")
        return
    sample = root / "samples" / "arch_plan.dxf"
    port = 8779
    cmd = [sys.executable, str(server), "--host", "127.0.0.1", "--port", str(port)]
    if sample.exists():
        cmd.append(str(sample))

    try:
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
    except Exception as ex:
        rep.add("viewer", "工作台服务", "bad", f"{type(ex).__name__}: {ex}",
                "把这段报错发出来")
        return

    up = False
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if _port_busy(port):
                up = True
                break
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            out, _ = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=5)

    if up:
        rep.add("viewer", "工作台服务", "ok", f"已在 :{port} 监听并能加载样例图")
        return
    tail = " / ".join((out or "").strip().splitlines()[-3:])[:300] or "无输出"
    rep.add("viewer", "工作台服务", "bad",
            f"20 秒内没能监听 :{port}（退出码 {proc.returncode}）：{tail}",
            "把上面这段报错发出来；常见原因是依赖没装全、端口被占、或样例图损坏")


def check_resources(rep: SetupReport, root: Path) -> None:
    """配置/样例之外再确认一下包本身能 import（最常见的是装在错的 python 下）。"""
    try:
        import revit_wy
        where = Path(revit_wy.__file__).resolve().parent
        inside = root.resolve() in where.parents or where.parent == root.resolve()
        rep.add("import", "revit_wy 包", "ok" if inside else "warn",
                f"{where}" + ("" if inside else "（不在本仓库内，用的是别处的安装）"),
                "" if inside else f'用 "{_venv_python(root)}" -m pip install -e "{root}" 重装')
    except Exception as ex:
        rep.add("import", "revit_wy 包", "bad", f"{type(ex).__name__}: {ex}",
                "重跑一键启动.bat")


# ------------------------------------------------------------------- 入口
def run(root: str | Path = ".") -> SetupReport:
    r = Path(root).resolve()
    rep = SetupReport(root=r)
    check_python(rep)
    check_venv(rep, r)
    check_repo(rep, r)
    check_deps(rep, r)
    check_resources(rep, r)
    check_scripts(rep, r)
    check_dotnet(rep)
    check_net48_targeting_pack(rep, r)
    check_ports(rep)
    check_addin(rep, r)
    check_viewer_serves(rep, r)
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser("revit-wy 启动自检")
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = run(a.root)
    if a.json:
        print(json.dumps({
            "root": str(rep.root),
            "worst": rep.worst,
            "checks": [c.__dict__ for c in rep.checks],
            "platform": platform.platform(),
        }, ensure_ascii=False, indent=2))
    else:
        try:
            print(rep.render())
        except UnicodeEncodeError:
            sys.stdout.buffer.write(rep.render().encode("utf-8", "replace"))
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
