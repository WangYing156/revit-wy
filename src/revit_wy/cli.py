"""revit-wy 命令行：把整条流水线拆成可单步调试的 6 个命令。

    revit-wy doctor                         环境自检（简版）
    revit-wy diag                           环境诊断（完整报告，可发给支持人员）
    revit-wy install-mcp                    写入 Cursor / WorkBuddy 的 MCP 配置（不破坏已有）
    revit-wy mock                           起一个假 Revit（没有 Revit 也能试跑）
    revit-wy demo                           一条命令演示全流程（自动起假 Revit）
    revit-wy dwg       a.dwg                 ★一键：DWG→构件台账（五阶段）
    revit-wy saving    a.ir.json             ★减负估算：省多少工时
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

    # 干跑也要连一下 Revit：不连就不知道模型里已有多少构件，
    # 也就没法告诉用户"这次会叠加"——那等于让用户在黑暗里按发送。
    existing = 0
    try:
        with RevitBridge(args.host, args.port) as b:
            existing = int(b.status().get("revit_wy_tagged", 0) or 0)
    except Exception as ex:
        if args.send:
            print(_c("err", f"连不上 Revit 插件：{ex}"))
            return 4
        print(_c("dim", f"  （未连上 Revit：{ex}；跳过已有构件核对）"))

    if existing and not args.replace:
        print(_c("warn", f"\n  模型里已有 {existing} 个上次翻模的构件。"))
        print(_c("warn", "  本次不删除它们，会叠加两份；要替换请加 --replace"))

    if args.dry_run or not args.send:
        print(_c("warn", "\n干跑：未写入 Revit。"
                         + ("确认后加 --send" if not existing else "确认后加 --send --replace")))
        return 0

    deleted = 0
    try:
        with RevitBridge(args.host, args.port) as b:
            if args.replace and existing:
                deleted = int((b.delete_tagged() or {}).get("deleted", 0))
                print(_c("dim", f"  已清理上次翻模构件 {deleted} 个"
                                f"（仅带 revit-wy 标记的，手工构件不受影响）"))
            res = b.build_ir(model.to_dict(), dry_run=False,
                             auto_save=not args.no_save)
    except Exception as ex:
        print(_c("err", f"写入 Revit 失败：{ex}"))
        if deleted:
            print(_c("err", f"注意：旧构件已删除 {deleted} 个，模型目前是空的，请重跑"))
        return 4
    _print(res)
    return 0


# ------------------------------------------------------------------ status
def cmd_panel(args) -> int:
    """启动本地预览服务。Revit 停靠面板会连这个端口取图层对照单。"""
    import runpy
    viewer = Path(__file__).resolve().parent.parent.parent / "tools" / "viewer" / "server.py"
    if not viewer.exists():
        print(_c("err", f"找不到预览服务: {viewer}"))
        return 1
    sys.argv = [str(viewer), args.path, "--host", args.host,
                "--port", str(args.port), "--profile", args.profile]
    print(_c("h", f"\n  预览服务  http://127.0.0.1:{args.port}/"))
    print(_c("dim", f"  Revit 停靠面板默认连 127.0.0.1:{args.port}（WyBackend.ViewerPort）\n"))
    runpy.run_path(str(viewer), run_name="__main__")
    return 0


# ------------------------------------------------------------------ dwg（一键）
STAGE_CN = {"convert": "转换 DWG→DXF", "scan": "图纸体检", "review": "数模对照",
            "clean": "图纸清洗", "build": "生成构件"}


def cmd_dwg(args) -> int:
    """DWG 优先的一键翻模（只到构件台账，写 Revit 仍走 build --send）。"""
    from .dwg_pipeline import run_dwg

    src = Path(args.path)
    if src.is_dir():
        from .dwg_pipeline import run_folder
        r = run_folder(src, args.profile, args.out,
                       pattern="*.dxf" if args.dxf_only else "*.dwg")
        _print({k: r[k] for k in ("total", "ok", "failed")})
        for row in r["results"]:
            mark = _c("ok", "✔") if row["ok"] else _c("err", "✘")
            print(f"  {mark} {Path(row['source']).name}  {row['counts'] or row['blockers']}")
        return 0 if r["failed"] == 0 else 1

    r = run_dwg(src, profile=args.profile, out_dir=args.out,
                accoreconsole=args.accoreconsole, base_level=args.base_level,
                max_manual_review=args.max_manual_review)

    print(_c("h", f"\n  {src.name}  →  {args.profile}"))
    for st in r.stages:
        mark = _c("ok", "✔") if st.ok else _c("err", "✘")
        name = STAGE_CN.get(st.name, st.name)
        line = f"  {mark} {name:<14}{st.seconds:>6.2f}s"
        if st.note:
            line += f"   {_c('dim', st.note)}"
        print(line)

    if r.ok:
        c = r.counts
        print(_c("ok", f"\n  构件台账：墙 {c['walls']}｜柱 {c['columns']}｜梁 {c['beams']}"
                       f"｜门 {c['doors']}｜窗 {c['windows']}｜板 {c['slabs']}"
                       f"｜轴网 {c['grids']}｜标高 {c['levels']}"))
        print(_c("dim", f"  数模对照：{r.review.get('markdown','')}"))
        print(_c("dim", f"  构件清单：{r.ir_path}"))
        sv = r.saving.get("seconds", {})
        if sv:
            print(f"  减负估算：手工 {sv['manual']/3600:.2f}h → 净省 "
                  f"{sv['net_saving']/3600:.2f}h（{r.saving['saving_pct']}%，"
                  f"明细 revit-wy saving <ir>）")
        for n in r.next_steps:
            print(f"   · {n}")
        if args.json:
            _print(r.as_dict())
        return 0

    print(_c("err", "\n  流水线中断，需要人工处理："))
    for b in r.blockers:
        print(f"   ! {b}")
    for n in r.next_steps:
        print(f"   · {n}")
    if args.json:
        _print(r.as_dict())
    return 2


# ------------------------------------------------------------------ saving
def cmd_saving(args) -> int:
    """把构件台账换算成工时账 —— 对领导讲减负时用这张表。"""
    from .effort import batch_estimate, estimate, load_baseline, write_markdown

    rows: List[Dict[str, Any]] = []
    for path in args.ir:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as ex:
            print(_c("err", f"读不了 {path}: {ex}"))
            return 2
        counts = {k: len(data.get(k) or []) for k in
                  ("levels", "grids", "walls", "columns", "beams",
                   "doors", "windows", "slabs")}
        rows.append({"drawing": Path(path).stem, "counts": counts,
                     "manual_review_layers": 0})

    if not rows:
        print(_c("err", "给一个 IR 文件：revit-wy saving model.ir.json"))
        return 2

    baseline, src = load_baseline(args.baseline)
    rep = (estimate(rows[0]["counts"], rows[0]["drawing"],
                    baseline=baseline, first_time=args.first_time).as_dict()
           if len(rows) == 1 else batch_estimate(rows, baseline=baseline))

    sec, days = rep["seconds"], rep["days"]
    print(_c("h", f"\n  减负估算（工时基准：{src}）"))
    print(f"  手工建模      {sec['manual']/3600:>7.2f} 小时   {days['manual']:>5.2f} 人天")
    print(f"  AI 处理       {sec['ai']/60:>7.1f} 分钟")
    print(f"  人工复核      {sec['review']/60:>7.1f} 分钟")
    print(f"  一次性投入    {sec['setup']/60:>7.1f} 分钟")
    color = "ok" if rep["worth_it"] else "warn"
    print(_c(color, f"  净节省        {sec['net_saving']/3600:>7.2f} 小时   "
                    f"{days['net_saving']:>5.2f} 人天   （{rep['saving_pct']}%）"))
    if not rep["worth_it"]:
        print(_c("dim", "  （单张小图首图可能不划算：规则调校是一次性的，后续复用）"))

    if args.md:
        p = write_markdown(rep, args.md)
        print(_c("dim", f"\n  Markdown 报告：{p}"))
    if args.json:
        _print(rep)
    return 0


# ------------------------------------------------------------------ mock/demo
def cmd_install_mcp(args) -> int:
    from .install_mcp import install
    rows = install(Path(".").resolve(), args.profile, args.python, args.force,
                   args.cursor_global, not args.no_workbuddy)
    for r in rows:
        mark = "OK" if r["ok"] == "true" else "X"
        print(_c("ok" if r["ok"] == "true" else "err",
                 f"  [{mark}] {r['client']:<12} {r['action']}"))
        print(_c("dim", f"        {r['path']}"))
    print(_c("dim", "\n  WorkBuddy 需在「连接器」里点一次 Trust；"
                    "Cursor 首次会询问是否启用该项目级 MCP。"))
    return 0 if all(r["ok"] == "true" for r in rows) else 1


def cmd_mock(args) -> int:
    from .mock_backend import port_open, serve_forever
    if port_open(args.port):
        print(_c("err", f"端口 {args.port} 已被占用——可能真 Revit 插件已经在跑，"
                        "或另一个 mock 没关"))
        return 2
    print(_c("h", f"\n  假 Revit 已启动：{args.host}:{args.port}"))
    print(_c("dim", "  它只做协议级仿真：校验 IR、统计构件、按真实错误码回包。"))
    print(_c("dim", "  现在可以另开窗口跑 revit-wy build x.dxf --send，或直接 revit-wy demo。"))
    print(_c("dim", "  Ctrl+C 结束。\n"))
    try:
        serve_forever(args.port, args.host)
    except KeyboardInterrupt:
        print(_c("dim", "\n  已停止。"))
    return 0


def cmd_demo(args) -> int:
    """一条命令把整条链路走一遍，包括写入（写进假 Revit）。"""
    from .mock_backend import port_open, start_background

    src = Path(args.path)
    if not src.exists():
        print(_c("err", f"找不到图纸：{src}"))
        print(_c("dim", "  仓库自带样例：samples/arch_plan.dxf、samples/struct_plan.dxf、"
                        "samples/messy_plan.dxf"))
        return 2

    srv = None
    if port_open(args.port):
        print(_c("warn", f"  端口 {args.port} 已有服务在监听，将直接用它"))
        print(_c("dim", "  （如果那是真 Revit 插件，本次演示会真的写入你的模型；"
                        "想写假模型就先关掉 Revit 插件）"))
    else:
        srv = start_background(args.port)
        print(_c("ok", f"  [1/4] 假 Revit 已启动 {args.port}"))
        print(_c("dim", "        没有 Revit 也能看到完整效果，只是不生成真实几何"))

    try:
        print(_c("h", f"\n  [2/4] 跑五阶段流水线：{src.name}"))
        from .dwg_pipeline import run_dwg
        out = Path(args.out) if args.out else Path(".demo")
        r = run_dwg(src, args.profile, out)
        if not r.ok:
            print(_c("err", "  流水线中断："))
            for b in r.blockers:
                print(f"   ! {b}")
            return 3
        for st in r.stages:
            print(f"   ✔ {st.name:<8}{st.seconds:>6.2f}s")
        c = r.counts
        print(_c("ok", f"  构件：墙 {c['walls']}｜柱 {c['columns']}｜梁 {c['beams']}"
                       f"｜门 {c['doors']}｜窗 {c['windows']}｜板 {c['slabs']}"
                       f"｜轴网 {c['grids']}｜标高 {c['levels']}"))

        print(_c("h", "\n  [3/4] 干跑 → 写入（模拟）"))
        from .bridge import RevitBridge
        with RevitBridge("127.0.0.1", args.port) as b:
            before = int(b.status().get("revit_wy_tagged", 0) or 0)
            if before:
                b.delete_tagged()
                print(_c("dim", f"        先清理上次的 {before} 个构件"))
            dry = b.build_ir(r.model.to_dict(), dry_run=True)
            print(_c("dim", f"        干跑：{dry.get('created')}"))
            res = b.build_ir(r.model.to_dict(), dry_run=False)
        if not res.get("ok"):
            print(_c("err", f"  写入失败：{res.get('errors')}"))
            return 4
        made = res.get("created", {})
        print(_c("ok", f"  写入成功：{made}"))

        sv = r.saving.get("seconds", {})
        if sv:
            print(_c("h", "\n  [4/4] 减负账"))
            print(f"   手工约 {sv['manual']/3600:.2f}h → 净省 {sv['net_saving']/3600:.2f}h"
                  f"（{r.saving['saving_pct']}%，见 revit-wy saving）")
            if r.saving.get("saving_pct", 0) <= 0:
                print(_c("dim", "   样例图只有十几个构件，规则调校是一次性成本，"
                                "所以单张小图为负是正常的；真实标准层见 docs/07"))

        print(_c("h", "\n  下一步你可以："))
        print(f"     · 看可视化：revit-wy panel {src}  → http://127.0.0.1:8770/")
        print(f"     · 看确认单：{r.review.get('markdown', '')}")
        print(f"     · 装好 Revit 2022 后，把 {src.name} 用 --send 真正写进去")
        print(_c("dim", "\n  注意：本次写入的是假 Revit（内存里的构件计数），"
                        "真实建模效果要在 Revit 里看。"))
        return 0
    finally:
        if srv is not None:
            srv.shutdown()
            print(_c("dim", "\n  假 Revit 已关闭。"))


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

    sv = sub.add_parser("saving", help="★减负估算：把构件台账换算成工时账")
    sv.add_argument("ir", nargs="+", help="IR json 文件（可多个，自动汇总）")
    sv.add_argument("--baseline", default="", help="自定义工时基准 YAML")
    sv.add_argument("--first-time", action="store_true", default=False,
                    help="算上首次规则调校成本（默认算后续复用）")
    sv.add_argument("--md", default="", help="输出 Markdown 报告路径")
    sv.add_argument("--json", action="store_true")
    sv.set_defaults(func=cmd_saving)

    w = sub.add_parser("dwg", help="★一键：DWG→构件台账（转换/体检/对照/清洗/建构件）")
    w.add_argument("path", help="DWG 或 DXF 文件，也可以是一个目录（批量）")
    w.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    w.add_argument("--out", default="", help="工作目录，默认 ~/.revit-wy/work/<图名>")
    w.add_argument("--accoreconsole", default="")
    w.add_argument("--base-level", default="")
    w.add_argument("--max-manual-review", type=int, default=0,
                   help="允许的待确认图层数上限，默认 0（宁停不乱建）")
    w.add_argument("--dxf-only", action="store_true", help="目录模式下只处理 DXF")
    w.add_argument("--json", action="store_true", help="额外打印完整 JSON")
    w.set_defaults(func=cmd_dwg)

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
    b.add_argument("--replace", action="store_true",
                   help="写入前清理上一次翻模构件（靠 revit-wy 标记，不动手工构件）")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=48880)
    b.set_defaults(func=cmd_build)

    pv = sub.add_parser("panel", help="启动数模对照可视化预览（供 Revit 停靠面板/浏览器共用）")
    pv.add_argument("path", nargs="?", default="", help="DXF 路径，可留空在页面里选")
    pv.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=8770)
    pv.set_defaults(func=cmd_panel)

    im = sub.add_parser("install-mcp", help="写入 MCP 客户端配置（合并，不覆盖）")
    im.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    im.add_argument("--python", default="", help="客户端要用的 python 绝对路径")
    im.add_argument("--force", action="store_true")
    im.add_argument("--cursor-global", action="store_true")
    im.add_argument("--no-workbuddy", action="store_true")
    im.set_defaults(func=cmd_install_mcp)

    mk = sub.add_parser("mock", help="起一个假 Revit（没有 Revit 也能试跑链路）")
    mk.add_argument("--port", type=int, default=48880)
    mk.add_argument("--host", default="127.0.0.1")
    mk.set_defaults(func=cmd_mock)

    dm = sub.add_parser("demo", help="一条命令演示全流程（自动起假 Revit）")
    dm.add_argument("path", nargs="?", default="samples/arch_plan.dxf")
    dm.add_argument("-p", "--profile", default="arch", choices=["arch", "struct", "mep"])
    dm.add_argument("--out", default="")
    dm.add_argument("--port", type=int, default=48880)
    dm.set_defaults(func=cmd_demo)

    dg = sub.add_parser("diag", help="环境诊断，生成可发送的报告")
    dg.add_argument("--root", default=".", help="仓库根目录")
    dg.add_argument("--out", default="")
    dg.add_argument("--port", type=int, default=48880)
    dg.add_argument("--viewer-port", type=int, default=8770)
    dg.set_defaults(func=lambda a: __import__(
        "revit_wy.diagnose", fromlist=["main"]).main(
        ["--root", a.root, "--out", a.out, "--port", str(a.port),
         "--viewer-port", str(a.viewer_port)]))

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
