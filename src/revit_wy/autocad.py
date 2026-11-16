"""AutoCAD 侧批处理：DWG → DXF + 图层规范化。

为什么不走 AutoCAD COM
---------------------
* COM (`win32com.client.Dispatch("AutoCAD.Application")`) 会启动完整 AutoCAD 界面，
  每张图几秒到十几秒，且会抢用户焦点。
* **accoreconsole.exe** 是无界面内核，单图 1~3 秒，可脚本化、可并行、不干扰用户，
  官方支持，随 AutoCAD 2013+ 一起安装。这是本方案的选择。

生成两个脚本：
  1. `dwg2dxf.scr`  —— SAVEAS 2018 DXF
  2. `normalize.scr` —— 可选：按映射表批量改图层名（在对图纸做标准化时用）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

ACCORECONSOLE_CANDIDATES = [
    r"C:\Program Files\Autodesk\AutoCAD 2024\accoreconsole.exe",
    r"C:\Program Files\Autodesk\AutoCAD 2023\accoreconsole.exe",
    r"C:\Program Files\Autodesk\AutoCAD 2022\accoreconsole.exe",
    r"C:\Program Files\Autodesk\AutoCAD 2021\accoreconsole.exe",
    r"C:\Program Files\Autodesk\AutoCAD 2020\accoreconsole.exe",
]


def find_accoreconsole(explicit: str = "") -> Optional[str]:
    if explicit and Path(explicit).exists():
        return explicit
    env = os.environ.get("ACCORECONSOLE", "")
    if env and Path(env).exists():
        return env
    which = shutil.which("accoreconsole")
    if which:
        return which
    for c in ACCORECONSOLE_CANDIDATES:
        if Path(c).exists():
            return c
    # 兜底：扫描 Autodesk 目录
    base = Path(r"C:\Program Files\Autodesk")
    if base.exists():
        for d in sorted(base.glob("AutoCAD *"), reverse=True):
            exe = d / "accoreconsole.exe"
            if exe.exists():
                return str(exe)
    return None


SAVEAS_SCRIPT = """_.SAVEAS
2018
{out}
_.QUIT
"""


def write_scripts(work_dir: Path, out_dxf: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    scr = work_dir / "dwg2dxf.scr"
    # AutoCAD 脚本中路径用正斜杠可避免转义问题
    scr.write_text(
        "_.FILEDIA\n0\n_.CMDDIA\n0\n_.SAVEAS\n_2018\n"
        + str(out_dxf).replace("\\", "/") + "\n_.QUIT\n",
        encoding="gbk",          # accoreconsole 中文版按系统 ANSI 读脚本
        errors="ignore",
    )
    return scr


@dataclass
class ConvertResult:
    dwg: str
    dxf: str = ""
    ok: bool = False
    error: str = ""
    seconds: float = 0.0


def convert_one(dwg: str | Path, out_dir: str | Path,
                accore: str, timeout: int = 180) -> ConvertResult:
    import time
    t0 = time.time()
    dwg = Path(dwg)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dxf = out_dir / (dwg.stem + ".dxf")
    scr_dir = out_dir / "_scr"
    scr = write_scripts(scr_dir, out_dxf)

    cmd = [accore, "/i", str(dwg), "/s", str(scr)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = out_dxf.exists() and out_dxf.stat().st_size > 1024
        return ConvertResult(
            dwg=str(dwg), dxf=str(out_dxf) if ok else "",
            ok=ok, seconds=round(time.time() - t0, 1),
            error="" if ok else (p.stdout or p.stderr or b"").decode("gbk", "ignore")[-500:],
        )
    except subprocess.TimeoutExpired:
        return ConvertResult(dwg=str(dwg), ok=False,
                             error=f"超时 {timeout}s", seconds=timeout)
    except Exception as ex:
        return ConvertResult(dwg=str(dwg), ok=False, error=str(ex),
                             seconds=round(time.time() - t0, 1))


def convert_folder(folder: str | Path, out_dir: str | Path = "",
                   recursive: bool = True, workers: int = 4,
                   accoreconsole: str = "") -> Dict:
    """并发把文件夹内所有 DWG 转 DXF。"""
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"目录不存在: {folder}")
    accore = find_accoreconsole(accoreconsole)
    if sys.platform != "win32" and not accore:
        return {
            "available": False,
            "reason": "accoreconsole 仅在 Windows + 已安装 AutoCAD 时可用",
            "alternative": [
                "在装有 AutoCAD 的机器上执行：revit-wy dwg2dxf <目录>",
                "或统一让上游提供 DXF（最省事）",
                "或安装 ODA File Converter（免费）后执行：revit-wy dwg2dxf --oda",
            ],
            "converted": [],
        }
    if not accore:
        raise FileNotFoundError(
            "未找到 accoreconsole.exe。请设置环境变量 ACCORECONSOLE "
            "或传入 accoreconsole 路径。"
        )

    pattern = "**/*.dwg" if recursive else "*.dwg"
    files = [p for p in folder.glob(pattern) if not p.name.startswith("~")]
    if not files:
        return {"available": True, "converted": [], "note": "未找到 DWG"}

    out_dir = Path(out_dir) if out_dir else folder / "dxf"
    results: List[ConvertResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(convert_one, f, out_dir, accore): f for f in files}
        for fu in as_completed(futs):
            results.append(fu.result())

    okc = sum(1 for r in results if r.ok)
    return {
        "available": True,
        "accoreconsole": accore,
        "total": len(results),
        "ok": okc,
        "failed": len(results) - okc,
        "out_dir": str(out_dir),
        "converted": [
            {"dwg": r.dwg, "dxf": r.dxf, "ok": r.ok, "s": r.seconds,
             "error": r.error}
            for r in sorted(results, key=lambda x: x.dwg)
        ],
    }


# ------------------------------------------------------------------ ODA 兜底
ODA_CANDIDATES = [
    r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
    "/usr/bin/ODAFileConverter",
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
]


def find_oda() -> Optional[str]:
    for c in ODA_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("ODAFileConverter")


def convert_folder_oda(folder: str | Path, out_dir: str | Path = "",
                       version: str = "ACAD2018", recursive: bool = True) -> Dict:
    """ODA File Converter 兜底（无 AutoCAD 时）。免费但需手动下载安装。"""
    exe = find_oda()
    folder = Path(folder)
    out = Path(out_dir) if out_dir else folder / "dxf"
    out.mkdir(parents=True, exist_ok=True)
    if not exe:
        return {"available": False,
                "reason": "未找到 ODAFileConverter",
                "download": "https://www.opendesign.com/guestfiles/oda_file_converter"}
    cmd = [exe, str(folder), str(out), version, "DXF", "0", "1",
           "*.DWG" if not recursive else "*.DWG"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception as ex:
        return {"available": True, "ok": False, "error": str(ex)}
    dxfs = sorted(p.name for p in out.glob("*.dxf"))
    return {"available": True, "ok": True, "out_dir": str(out),
            "count": len(dxfs), "files": dxfs[:50]}
