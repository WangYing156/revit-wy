"""revit-wy 命令行：把整条流水线拆成可单步调试的 6 个命令。

    revit-wy doctor                         环境自检
    revit-wy dwg2dxf  <dir>                 批量 DWG→DXF（accoreconsole）
    revit-wy scan      a.dxf                图纸体检
    revit-wy review    a.dxf -p arch        生成“数模对照”确认单  ← 需求1
    revit-wy confirm   --review review.csv  回灌确认结果
    revit-wy build     a.dxf --dry-run      构建 IR + 可选写入 Revit
    revit-wy build     a.dxf --send         真正写入 Revit 2022
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from . import config as cfg
from .bridge import RevitBridge, probe
from .dwg import clean as dwg_clean
from .dwg import extract, layer_thickness_from_name, scan
from .ir import assign_host_walls, build as build_ir
from .mapping import LayerMapper, load_aliases, save_aliases
from .report import read_overrides, rules_summary, write_review

ALIAS_FILE = cfg.USER_CONFIG_DIR / "aliases.json"
STYLE = {
    "h": "\033[1;36m", "ok": "\033[1;32m", "warn": "\033[1;33m",
    "err": "\033[1;31m", "dim": "\033[2m", "0": "\033[0m",
}


def _c(k: str, s: str) -> str:
    return f"{STYLE.get(k,'')}{s}{STYLE['0']}" if sys.stdout.isatty() else s


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ doctor
def cmd_doctor(args) -> int:
    import ezdxf
    from .autocad import find_accoreconsole, find_oda
    info = {
        "revit_wy": __version__,
        "python": sys.version.split()[0],
        "ezdxf": ezdxf.__version__,
        "config_dir": str(cfg.CONFIG_DIR),
        "user_dir": str(cfg.USER_CONFIG_DIR),
        "autocad": {"accoreconsole": find_accoreconsole() or "未找到",
                    "oda": find_oda() or "未找到"},
        "revit_bridge": probe(args.host, args.port),
    }
    _print(info)
    return 0 if info["revit_bridge"].get("connected") or args.allow_offline else 0


# ------------------------------------------------------------------ dwg2dxf
def cmd_dwg2dxf(args) -> int:
    from .autocad import convert_folder, convert_folder_oda
    if args.oda:
        _print(convert_folder_oda(args.folder, args.out, recursive=not args.no_recursive))
    else:
        _print(convert_folder(args.folder, args.out,
                              recursive=not args.no_recursive,
                              workers=args.workers,
                              accoreconsole=args.accoreconsole or ""))
    return 0


# ------------------------------------------------------------------ scan
def cmd_scan(args) -> int:
    info = scan(args.path)
    ext = info.extent
    _print({
        "path": info.path,
        "dxf_version": info.dxf_version,
        "units": {"insunits": info.insunits, "to_mm": info.unit_to_mm},
        "extent_mm": [round(v, 1) for v in ext],
        "total_entities": info.total_entities,
        "layer_count": len(info.layers),
        "top_layers": [{"name": l.name, "count": l.entity_count,
                        "kinds": dict(sorted(l.kinds.items(), key=lambda t: -t[1])[:3])}
                       for l in info.layers[:40]],
        "blocks": info.block_names[:40],
    })
    return 0


# ------------------------------------------------------------------ review
def cmd_review(args) -> int:
    info = scan(args.path)
    mapper = LayerMapper(cfg.load_mapping(args.profile),
                         load_aliases(ALIAS_FILE))
    out = Path(args.out) if args.out else Path(args.path).with_suffix("") / "review"
    paths = write_review(info, mapper, out, rules_summary(cfg.load_rules(args.rules)))
    results = [mapper.match(l.name) for l in info.layers]
    todo = [r for r in results if r.action == "manual_review"]
    print(_c("h", f"\n✅ 数模对照单已生成：{out}"))
    for k, v in paths.items():
        print(f"   {k:10s} {v}")
    print(f"\n   图层 {len(results)} 个，其中 {_c('warn', str(len(todo)))} 个需要确认")
    if todo:
        print(_c("dim", "\n   待确认："))
        for r in todo[:20]:
            print(f"     - {r.layer!r:36s} 猜测={r.component} ({r.confidence:.2f})")
        print(_c("dim", f"\n   在 {paths['csv']} 的 override_component 列填写后执行："
                        f"\n   revit-wy confirm --review {paths['csv']}"))
    return 0


# ------------------------------------------------------------------ confirm
def cmd_confirm(args) -> int:
    mapper = LayerMapper(cfg.load_mapping(args.profile), load_aliases(ALIAS_FILE))
    applied: Dict[str, str] = {}
    if args.review:
        applied.update(read_overrides(args.review))
    for pair in args.set or []:
        if "=" not in pair:
            print(_c("err", f"格式错误: {pair}，应为 图层=构件"))
            return 2
        k, v = pair.split("=", 1)
        applied[k.strip()] = v.strip()
    if not applied:
        print(_c("warn", "没有需要确认的条目"))
        return 0
    for layer, comp in applied.items():
        mapper.learn(layer, comp)
    save_aliases(mapper.export_aliases(), ALIAS_FILE)
    print(_c("ok", f"已确认 {len(applied)} 个图层，写入 {ALIAS_FILE}"))
    for k, v in applied.items():
        print(f"   {k} → {v}")
    return 0


# ------------------------------------------------------------------ build
def cmd_build(args) -> int:
    mapper = LayerMapper(cfg.load_mapping(args.profile), load_aliases(ALIAS_FILE))
    rules = cfg.load_rules(args.rules)
    work = Path(args.out) if args.out else Path(args.path).with_suffix("") / "build"
    work.mkdir(parents=True, exist_ok=True)

    src = args.path
    if not args.no_cleanup:
        cres = dwg_clean(args.path, mapper, work / f"{Path(args.path).stem}_clean.dxf")
        src = cres.output
        print(_c("dim", f"清洗完成 → {src}"))
        for n in cres.notes:
            print(_c("dim", f"  · {n}"))

    prims = extract(src, mapper)
    lt: Dict[str, float] = {}
    for layer in {p.layer for p in prims.get("wall", [])}:
        th = layer_thickness_from_name(layer, mapper.match(layer).extras)
        if th:
            lt[layer] = th

    model = build_ir(prims, rules, profile=args.profile, source=args.path,
                     base_level=args.base_level or "1层", walls_layer_thickness=lt)
    assign_host_walls(model)
    ir_path = model.save(work / "model.ir.json")

    counts = model.counts()
    print(_c("h", "\n构件统计"))
    for k, v in counts.items():
        print(f"   {k:10s} {v}")
    print(f"\n   IR → {ir_path}")

    unresolved = [r for r in (mapper.match(p.layer) for p in
                              prims.get("wall", [])) if r.action == "manual_review"]
    if args.auto and unresolved:
        print(_c("err", f"--auto 模式下仍有 {len(unresolved)} 个图层未确认，已中止"))
        return 3

    if args.dry_run or not args.send:
        print(_c("warn", "\n干跑：未写入 Revit。确认后加 --send"))
        return 0

    try:
        with RevitBridge(args.host, args.port) as b:
            res = b.build_ir(model.to_dict(), dry_run=False, auto_save=not args.no_save)
    except Exception as ex:
        print(_c("err", f"写入 Revit 失败：{ex}"))
        return 4
    _print(res)
    return 0


# ------------------------------------------------------------------ status
def cmd_status(args) -> int:
    _print(probe(args.host, args.port))
    return 0


# ------------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("revit-wy", description=f"AutoCAD→Revit 2022 一键翻模 v{__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=48880)
    d.add_argument("--allow-offline", action="store_true")
    d.set_defaults(func=cmd_doctor)

    c = sub.add_parser("dwg2dxf", help="DWG 批量转 DXF")
    c.add_argument("folder")
    c.add_argument("--out", default="")
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--oda", action="store_true", help="用 ODA File Converter")
    c.add_argument("--no-recursive", action="store_true")
    c.add_argument("--accoreconsole", default="")
    c.set_defaults(func=cmd_dwg2dxf)

    s = sub.add_parser("scan", help="图纸体检")
    s.add_argument("path")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("review", help="生成数模对照确认单")
    r.add_argument("path")
    r.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    r.add_argument("--rules", default=None)
    r.add_argument("--out", default="")
    r.set_defaults(func=cmd_review)

    f = sub.add_parser("confirm", help="回灌图层确认结果")
    f.add_argument("--review", default="")
    f.add_argument("--set", action="append", help="图层=构件，可多次")
    f.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    f.set_defaults(func=cmd_confirm)

    b = sub.add_parser("build", help="构建 IR 并（可选）写入 Revit")
    b.add_argument("path")
    b.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    b.add_argument("--rules", default=None)
    b.add_argument("--out", default="")
    b.add_argument("--base-level", default="",
                   help="起始标高名；留空自动取本次生成的第一层")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--send", action="store_true", help="真正写入 Revit")
    b.add_argument("--auto", action="store_true", help="零待确认图层才继续")
    b.add_argument("--no-cleanup", action="store_true")
    b.add_argument("--no-save", action="store_true")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=48880)
    b.set_defaults(func=cmd_build)

    st = sub.add_parser("status", help="Revit 插件状态")
    st.add_argument("--host", default="127.0.0.1")
    st.add_argument("--port", type=int, default=48880)
    st.set_defaults(func=cmd_status)

    m = sub.add_parser("mcp", help="启动 MCP 服务（stdio）")
    m.add_argument("--profile", default="arch", choices=["arch", "struct", "mep"])
    m.set_defaults(func=lambda a: _run_mcp(a))
    return ap


def _run_mcp(args) -> int:
    from .mcp_server import main as mcp_main
    mcp_main(["--profile", args.profile])
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
