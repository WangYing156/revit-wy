"""一键启动的真正大脑：Python 主控，.bat 只负责找到 Python 并把它叫起来。

为什么要从 .bat 搬过来
----------------------
前三轮"一键启动跑不起来"全部是 cmd.exe 层面的问题：
      UTF-8 中文被 GBK 读坏 → 命令乱码
      LF 换行 → goto/label 失效
      `-e "%ROOT%."` 拼成 `F:\repo\.` → pip 失败
      括号块里 set → 变量吞掉右括号
      失败只 echo 一行 → 用户拿不到可用信息

这些坑的共同点：**cmd 没有异常、没有栈、没有回滚，也没有日志**。
搬到 Python 后：每一步有 try/except、有耗时、有完整输出、
有结构化日志落盘，失败时能明确分类并给出可执行的下一步。

设计原则
--------
1. 任何一步失败都不静默。要么重试，要么如实报告并继续能继续的部分。
2. 每一条失败都分类：环境问题 / 网络问题 / 编译问题 / 权限问题。
   分类决定给用户的"下一步"是哪句，不能一句"失败了"打发。
3. 全程写日志到 reports\\setup-<时间>.log，UTF-8 BOM（记事本能直接开）。
   用户只要把这个文件发出来，就能定位问题——不依赖他复述屏幕。
4. 结束时永远给一个**三选一**，而不是一句"请检查环境"。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

VIEWER_PORT = 8770


# ------------------------------------------------------------------ 日志
class Log:
    """同时写屏幕和文件。屏幕用中文，文件用 UTF-8 BOM。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buf: List[str] = []
        self._fh = open(self.path, "w", encoding="utf-8-sig", newline="\r\n")
        self.write(f"revit-wy setup log  {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.write(f"platform : {platform.platform()}")
        self.write(f"python   : {sys.version.split()[0]}  {sys.executable}")
        self.write("=" * 70)

    def write(self, msg: str = "") -> None:
        self._buf.append(msg)
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            # 老终端（GBK 控制台）打不出某些字符时降级，但文件里保留完整内容
            print(msg.encode(sys.stdout.encoding or "utf-8", "replace")
                  .decode(sys.stdout.encoding or "utf-8", "replace"), flush=True)
        if self._fh.closed:          # 退出路径上可能已关过，不能再抛
            return
        try:
            self._fh.write(msg + "\n")
            self._fh.flush()
        except (ValueError, OSError):
            pass

    def step(self, n: int, total: int, title: str) -> None:
        self.write("")
        self.write(f"[{n}/{total}] {title}")

    def ok(self, msg: str) -> None:
        self.write(f"      OK   {msg}")

    def warn(self, msg: str) -> None:
        self.write(f"      WARN {msg}")

    def fail(self, msg: str) -> None:
        self.write(f"      FAIL {msg}")

    def close(self) -> None:
        if self._fh.closed:
            return
        self.write("=" * 70)
        self.write(f"log saved: {self.path}")
        try:
            self._fh.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 结果
@dataclass
class Outcome:
    ok: bool = True
    blockers: List[str] = field(default_factory=list)      # 必须解决
    degraded: List[str] = field(default_factory=list)     # 能用但降级
    next_steps: List[str] = field(default_factory=list)   # 给用户的动作

    def block(self, msg: str) -> None:
        self.ok = False
        self.blockers.append(msg)

    def degrade(self, msg: str) -> None:
        self.degraded.append(msg)


# ------------------------------------------------------------------ 工具
def _run(cmd: Sequence[str], log: Log, cwd: Optional[Path] = None,
         timeout: int = 1800, echo: bool = False) -> "subprocess.CompletedProcess[str]":
    """跑一个子进程，把输出同时收进日志。超时/异常统一转成 CompletedProcess。"""
    shown = " ".join(cmd)
    log.write(f"      $ {shown}")
    t0 = time.time()
    try:
        p = subprocess.run(list(cmd), cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        log.write(f"      ! 超时（>{timeout}s）：{shown}")
        return subprocess.CompletedProcess(cmd, -9, stdout="", stderr=f"timeout after {timeout}s")
    except FileNotFoundError as ex:
        log.write(f"      ! 找不到可执行文件：{ex}")
        return subprocess.CompletedProcess(cmd, -1, stdout="", stderr=str(ex))
    except Exception as ex:
        log.write(f"      ! 启动失败：{type(ex).__name__}: {ex}")
        return subprocess.CompletedProcess(cmd, -1, stdout="", stderr=str(ex))

    dt = time.time() - t0
    out = (p.stdout or "").rstrip()
    err = (p.stderr or "").rstrip()
    if echo or p.returncode != 0:
        for line in out.splitlines()[-40:]:
            log.write(f"        | {line}")
        for line in err.splitlines()[-25:]:
            log.write(f"        ! {line}")
    log.write(f"      退出码 {p.returncode}（{dt:.1f}s）")
    return p


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _which_python() -> List[List[str]]:
    """返回若干种"能启动 Python"的命令，按优先级。"""
    cands: List[List[str]] = []
    if os.name == "nt":
        cands.append(["py", "-3"])
    cands.append([sys.executable])          # 当前解释器（最可靠）
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            cands.append([p])
    # 去重
    seen, out = set(), []
    for c in cands:
        key = tuple(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ------------------------------------------------------------------ 各步骤
def step_venv(root: Path, log: Log, out: Outcome) -> Optional[Path]:
    """建 .venv。已存在就直接用。"""
    vpy = _venv_python(root)
    if vpy.exists():
        log.ok(f".venv 已存在：{vpy}")
        return vpy

    log.write("      创建 .venv（约 1 分钟）...")
    last_err = ""
    for cmd in _which_python():
        p = _run(cmd + ["-m", "venv", str(root / ".venv")], log, timeout=600)
        if vpy.exists():
            log.ok(f"已创建：{vpy}（用 {' '.join(cmd)}）")
            return vpy
        last_err = (p.stderr or "").strip()[-400:]
    log.fail(f"创建 .venv 失败：{last_err or '未知原因'}")
    out.block("无法创建 Python 虚拟环境 .venv")
    out.next_steps.append(
        "手动建一次虚拟环境，看它到底报什么：\n"
        f'        py -3 -m venv "{root}"\\.venv')
    return None


DEPS = (("ezdxf", "读 DXF"), ("yaml", "读配置"), ("mcp", "MCP 服务"))


def _deps_ok(vpy: Path, log: Log) -> bool:
    code = "import ezdxf, yaml, mcp"
    p = _run([str(vpy), "-c", code], log, timeout=120)
    return p.returncode == 0


def step_deps(root: Path, vpy: Path, log: Log, out: Outcome) -> None:
    if _deps_ok(vpy, log):
        log.ok("依赖齐全（ezdxf / PyYAML / mcp）")
        return

    log.write("      安装依赖（1-3 分钟）...")
    _run([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "-q"], log, timeout=600)

    attempts = [
        ("直连 PyPI", [str(vpy), "-m", "pip", "install", "-e", str(root)]),
        ("清华源", [str(vpy), "-m", "pip", "install", "-e", str(root),
                    "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]),
        ("只用预编译包", [str(vpy), "-m", "pip", "install", "-e", str(root),
                          "--only-binary=:all:", "--prefer-binary"]),
    ]
    for label, cmd in attempts:
        log.write(f"      尝试：{label}")
        p = _run(cmd, log, timeout=1800)
        if p.returncode == 0 and _deps_ok(vpy, log):
            log.ok(f"依赖安装成功（{label}）")
            break
    else:
        log.fail("三种安装方式都失败")
        out.block("Python 依赖装不上（ezdxf / PyYAML / mcp）")
        out.next_steps.append(
            "多半是网络或公司代理。手动试一次看具体报错：\n"
            f'        "{vpy}" -m pip install -e "{root}" -i https://pypi.tuna.tsinghua.edu.cn/simple')
        return

    # pywin32 只影响 .dwg 输入，缺了不算错
    p = _run([str(vpy), "-c", "import win32com.client"], log, timeout=60)
    if p.returncode != 0:
        log.warn("未安装 pywin32 —— 只影响 .dwg 输入（.dxf 完全可用）")
        out.degrade(".dwg → .dxf 转换需要 pywin32（.dxf 输入不受影响）")


def _installed_revits() -> List[str]:
    if os.name != "nt":
        return []
    found = set()
    for base in (r"C:\Program Files\Autodesk", r"C:\Program Files (x86)\Autodesk"):
        p = Path(base)
        if not p.is_dir():
            continue
        for d in p.glob("Revit 20*"):
            v = d.name.replace("Revit ", "").strip()
            if v.isdigit() and int(v) >= 2022:
                found.add(v)
    # 注册表兜底：装在非默认盘时目录探测会漏
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Autodesk\Revit") as k:
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(k, i)
                        except OSError:
                            break
                        i += 1
                        mt = sub.split()[0]
                        if mt.isdigit() and int(mt) >= 2022:
                            found.add(mt)
            except OSError:
                pass
    except Exception:
        pass
    return sorted(found)


def _addin_deployed(v: str) -> bool:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return False
    d = Path(appdata) / "Autodesk" / "Revit" / "Addins" / v
    return (d / "RevitWy.Plugin.addin").exists() and \
           (d / "revit-wy" / "RevitWy.Plugin.dll").exists()


def step_addin(root: Path, log: Log, out: Outcome) -> None:
    versions = _installed_revits()
    if not versions:
        log.warn("本机未检测到 Revit 2022+ —— 跳过插件编译")
        out.degrade("未装 Revit：除『写入模型 / 模型体检』外全部可用")
        return
    log.ok("已装 Revit：" + "、".join(versions))

    missing = [v for v in versions if not _addin_deployed(v)]
    if not missing:
        log.ok("插件已部署：" + "、".join(versions))
        return

    log.write("      需要编译部署：" + "、".join(missing))

    ps1 = root / "scripts" / "build-addin.ps1"
    if not ps1.exists():
        log.fail(f"缺 {ps1}")
        out.block("找不到 scripts/build-addin.ps1（仓库不完整）")
        return

    if not shutil.which("dotnet"):
        log.fail("未找到 dotnet SDK")
        out.block("找不到 dotnet 命令，无法编译 Revit 插件")
        out.next_steps.append(
            "装 .NET SDK 8 或更高（6 分钟）：https://dotnet.microsoft.com/download\n"
            "       装完重跑一键启动；不装也能用除『写入 Revit』以外的全部功能")
        return

    base = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ps1), "-Deploy", "-RevitVersions", ",".join(missing)]

    # 先 NuGet（不需要 .NET Framework 4.8 开发包），失败再回退本地 API
    p = _run(base + ["-UseNuget"], log, timeout=1800, echo=True)
    still = [v for v in missing if not _addin_deployed(v)]
    if not still:
        log.ok("插件编译并部署成功（NuGet API）")
        return

    log.warn("NuGet 路径未成功，改用本地 RevitAPI.dll 重试")
    p2 = _run(base, log, timeout=1800, echo=True)
    still = [v for v in missing if not _addin_deployed(v)]
    if not still:
        log.ok("插件编译并部署成功（本地 API）")
        return

    # 到这里说明两种方式都失败。按日志特征分类给建议，而不是一句"失败了"。
    blob = ((p.stdout or "") + (p.stderr or "") +
            (p2.stdout or "") + (p2.stderr or ""))
    log.fail(f"插件编译失败（未部署：{'、'.join(still)}）")
    out.block("Revit 插件编译失败，Revit 里不会出现 revit-wy 页签")

    if "MSB3644" in blob or "引用程序集" in blob:
        out.next_steps.append(
            "缺 .NET Framework 4.8 的引用程序集（和 .NET SDK 不是一回事）。三选一：\n"
            "        a) 装 .NET Framework 4.8 Developer Pack（约 70MB，最快）\n"
            "           https://dotnet.microsoft.com/download/dotnet-framework/net48\n"
            "        b) 装 VS Build Tools，勾选 \".NET Framework 4.8 targeting pack\"\n"
            "        c) 也可以先不编插件：翻模/体检/报告等功能都能用，只是不能写进 Revit")
    elif "NU1101" in blob or "Unable to load the service index" in blob or "401" in blob:
        out.next_steps.append(
            "拉不到 NuGet 包（网络/代理）。试：\n"
            f'        "{_venv_python(root)}" 无关，请改用本地 API：\n'
            f"        powershell -File scripts\\build-addin.ps1 -Deploy -RevitVersions {','.join(still)}\n"
            "       若外网受限，装 VS Build Tools 后走本地 API 路径")
    elif "CS" in blob and "error" in blob.lower():
        out.next_steps.append(
            "是 C# 源码编译错误（不是环境问题）。把 reports 里的 setup 日志发出来，\n"
            "       里面有完整的 error CSxxxx 行号")
    else:
        out.next_steps.append(
            "把 reports\\setup-*.log 发出来，里面完整记录了编译输出")


def step_mcp(root: Path, vpy: Path, log: Log, out: Outcome) -> None:
    p = _run([str(vpy), "-m", "revit_wy.install_mcp",
              "--root", str(root), "--python", str(vpy)], log, timeout=300)
    if p.returncode == 0:
        log.ok("Cursor / WorkBuddy 的 MCP 配置已写入")
    else:
        log.warn("MCP 配置写入不完整（不影响其他功能）")
        out.degrade("MCP 客户端配置没写全，Cursor/WorkBuddy 里可能看不到 revit-wy 工具")


def step_selftest(root: Path, vpy: Path, log: Log, out: Outcome) -> None:
    sample = root / "samples" / "arch_plan.dxf"
    if not sample.exists():
        log.warn("samples 里没有样例图，跳过自检")
        return
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "revit-wy-selftest"
    p = _run([str(vpy), "-m", "revit_wy.cli", "dwg", str(sample),
              "--out", str(tmp)], log, timeout=600)
    if p.returncode == 0:
        log.ok("五阶段流水线自检通过")
    else:
        log.warn("流水线自检失败（工作台仍可用）")
        out.degrade("图纸流水线自检未通过，建议双击 一键诊断.bat 看细节")


def step_ports(log: Log, out: Outcome) -> bool:
    """返回 True 表示 8770 可用（可以起工作台）。"""
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    busy = s.connect_ex(("127.0.0.1", VIEWER_PORT)) == 0
    s.close()
    if busy:
        log.warn(f"端口 {VIEWER_PORT} 已被占用（可能是上次没关的工作台）")
        out.degrade(f"工作台没有新起，端口 {VIEWER_PORT} 被占用")
        return False
    log.ok(f"端口 {VIEWER_PORT} 空闲")
    return True


# ------------------------------------------------------------------ 收尾
def print_summary(out: Outcome, log: Log, log_path: Path) -> int:
    """收尾：结论 + 下一步 + 日志路径。日志在这里最后一次落盘并关闭。"""
    log.write("")
    log.write("=" * 70)
    if out.ok and not out.degraded:
        log.write("  结论：全部就绪。")
    elif out.ok:
        log.write("  结论：核心功能可用，以下功能降级：")
        for d in out.degraded:
            log.write(f"    - {d}")
    else:
        log.write("  结论：有必须先解决的问题：")
        for b in out.blockers:
            log.write(f"    X {b}")
        for d in out.degraded:
            log.write(f"    - {d}")

    if out.next_steps:
        log.write("")
        log.write("  下一步（按顺序）：")
        for i, s in enumerate(out.next_steps, 1):
            log.write(f"    {i}) {s}")

    log.write("")
    log.write("  出问题时：把下面这个文件发出来即可，不必复述屏幕")
    log.write(f"    {log_path}")
    log.close()
    return 0 if out.ok else 2


def run(root: str | Path, no_viewer: bool = False,
        log_path: Optional[str] = None) -> int:
    root = Path(root).resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lp = Path(log_path) if log_path else root / "reports" / f"setup-{stamp}.log"
    log = Log(lp)
    out = Outcome()

    log.write("")
    log.write("=" * 70)
    log.write("  revit-wy 一键启动  |  AutoCAD -> Revit 2022+")
    log.write("=" * 70)

    try:
        log.step(1, 6, "Python 环境")
        vpy = step_venv(root, log, out)
        if vpy is None:
            return print_summary(out, log, lp)

        log.step(2, 6, "依赖")
        step_deps(root, vpy, log, out)

        log.step(3, 6, "启动自检")
        from . import setup_check
        rep = setup_check.run(root)
        for c in rep.checks:
            (log.ok if c.level == "ok" else
             log.warn if c.level == "warn" else log.fail)(f"{c.label} —— {c.detail}")
        if rep.worst == "bad":
            for c in rep.checks:
                if c.level == "bad" and c.fix:
                    out.next_steps.append(c.fix)

        log.step(4, 6, "Revit 插件")
        step_addin(root, log, out)

        log.step(5, 6, "MCP 配置与流水线自检")
        step_mcp(root, vpy, log, out)
        step_selftest(root, vpy, log, out)

        log.step(6, 6, "工作台")
        if no_viewer:
            log.write("      已指定 --no-viewer，跳过")
        elif step_ports(log, out):
            log.write("      启动工作台（关闭这个窗口即停止）...")
            log.write(f"      http://127.0.0.1:{VIEWER_PORT}/")
            log.close()          # 先落盘，前台服务期间日志不再增长
            _launch_viewer(root, vpy)
            return 0
    except KeyboardInterrupt:
        log.write("\n  已中断")
        return 130
    except Exception:
        log.fail("启动器自身异常")
        for line in traceback.format_exc().splitlines():
            log.write(f"        {line}")
        out.block("一键启动脚本自身出错（见上面的栈）")
        out.next_steps.append(f"把 {lp} 发出来，里面有完整调用栈")
    finally:
        pass

    return print_summary(out, log, lp)


def _launch_viewer(root: Path, vpy: Path) -> None:
    """前台跑工作台：用户关掉窗口就停，符合"这个窗口就是服务"的预期。"""
    server = root / "tools" / "viewer" / "server.py"
    cmd = [str(vpy), str(server), "--host", "127.0.0.1", "--port", str(VIEWER_PORT)]
    sample = root / "samples" / "arch_plan.dxf"
    if sample.exists():
        cmd.append(str(sample))

    # 2 秒后再开浏览器，避免打开时端口还没起来
    try:
        import threading
        import webbrowser
        threading.Timer(
            2.0, lambda: webbrowser.open(f"http://127.0.0.1:{VIEWER_PORT}/")).start()
    except Exception:
        pass

    try:
        subprocess.call(cmd)
    except KeyboardInterrupt:
        pass
    print("\n  工作台已停止。")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser("revit-wy 一键启动")
    ap.add_argument("--root", default=".")
    ap.add_argument("--no-viewer", action="store_true")
    ap.add_argument("--log", default="")
    a = ap.parse_args(argv)
    return run(a.root, a.no_viewer, a.log or None)


if __name__ == "__main__":
    raise SystemExit(main())
