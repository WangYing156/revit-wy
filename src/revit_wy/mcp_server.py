"""MCP Server（stdio）：把 AutoCAD→Revit 翻模能力暴露给 Cursor / WorkBuddy。

工具设计原则
-----------
* **每个工具都做一件小而确定的事**，不把整条流水线塞进一个 tool。
  因为 LLM 需要中间结果做判断（尤其图层映射的确认环节）。
* **写操作默认 dry_run**，必须显式 `dry_run=false` 且 `confirm=true` 才落模型。
* 返回值一律 `{"ok": bool, ...}`，错误不抛异常而是结构化返回，
  这样客户端能自我纠正（cursor 会把 error 文本喂回模型）。

启动：python -m revit_wy.mcp_server --profile arch
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:                                   # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                    # mcp 1.x
    try:
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError:                # 更早的版本
        from fastmcp import FastMCP as _Server

from . import __version__
from . import config as cfg
from .bridge import RevitBridge, probe
from .dwg import extract, scan
from .ir import _target_revit
from .ir import assign_host_walls, build as build_ir
from .mapping import LayerMapper, load_aliases, save_aliases
from .report import read_overrides, rules_summary, write_review

STATE_DIR = cfg.USER_CONFIG_DIR
ALIAS_FILE = STATE_DIR / "aliases.json"
WORK_DIR = STATE_DIR / "work"

# instructions 会作为服务说明推给客户端，AI 靠它决定"先调哪个工具"。
# 没有它时模型容易直接调 rwy_build，然后在"还没有 IR"上撞一次。
mcp = _Server(
    "revit-wy",
    version=__version__,
    instructions=(
        "AutoCAD 图纸转 Revit 2022+ 构件的工具集。\n"
        "推荐路径：① 先 rwy_dwg_to_model 一步跑完转换/体检/对照/清洗/建构件；"
        "② 若返回 blockers（如「有 N 个图层未能自动归类」），把图层名和你的判断"
        "用 rwy_confirm_mapping 确认后重试；③ rwy_build(dry_run=true) 看将建什么，"
        "**必须让用户确认**；④ rwy_build(dry_run=false, confirm=true) 才真正写入。\n"
        "硬约束：不要跳过干跑；模型里已有旧构件时，问用户要不要 replace_existing=true。"
    ),
)

# 进程内会话状态：一次翻模过程的中间产物
_S: Dict[str, Any] = {
    "profile": "arch",
    "rules": None,
    "mapping": None,
    "mapper": None,
    "drawing": None,
    "ir": None,
    "workspace": None,
}


def _mapper(profile: Optional[str] = None) -> LayerMapper:
    profile = profile or _S.get("profile", "arch")
    if _S.get("mapper") is None or _S.get("profile") != profile:
        mapping = cfg.load_mapping(profile)
        _S["mapper"] = LayerMapper(mapping, load_aliases(ALIAS_FILE))
        _S["mapping"] = mapping
        _S["profile"] = profile
    return _S["mapper"]


def _rules(custom: Optional[str] = None) -> Dict[str, Any]:
    if _S.get("rules") is None or custom:
        _S["rules"] = cfg.load_rules(custom)
    return _S["rules"]


def _ok(**kw) -> Dict[str, Any]:
    return {"ok": True, **kw}


def _err(msg: str, **kw) -> Dict[str, Any]:
    return {"ok": False, "error": msg, **kw}


# ============================================================== 工具
@mcp.tool()
def rwy_doctor(host: str = "127.0.0.1", port: int = 48880) -> Dict[str, Any]:
    """环境自检：Python/ezdxf 版本、配置、Revit 插件连通性。翻模第一步先跑这个。"""
    import ezdxf
    return _ok(
        version=__version__,
        python=sys.version.split()[0],
        ezdxf=ezdxf.__version__,
        config_dir=str(cfg.CONFIG_DIR),
        user_dir=str(cfg.USER_CONFIG_DIR),
        profiles=["arch", "struct", "mep"],
        revit=probe(host, int(port)),
    )


@mcp.tool()
def rwy_scan_drawing(path: str) -> Dict[str, Any]:
    """扫描 DXF：单位、图幅、图层清单与实体分布、图块名。用于判断图纸可翻模性。

    path 必须是 .dxf。若只有 .dwg，先调 rwy_dwg2dxf。
    """
    try:
        info = scan(path)
    except Exception as ex:
        return _err(str(ex))
    _S["drawing"] = info
    ext = info.extent
    return _ok(
        path=info.path,
        dxf_version=info.dxf_version,
        units={"insunits": info.insunits, "to_mm": info.unit_to_mm},
        extent_mm=[round(v, 1) for v in ext],
        size_m=[round((ext[2] - ext[0]) / 1000, 1), round((ext[3] - ext[1]) / 1000, 1)],
        total_entities=info.total_entities,
        layer_count=len(info.layers),
        layers=[
            {"name": l.name, "count": l.entity_count,
             "kinds": sorted(l.kinds.items(), key=lambda t: -t[1])[:4]}
            for l in info.layers[:80]
        ],
        blocks=info.block_names[:60],
        warning=("图幅超过 200m，疑似单位非 mm 或多图拼在一起"
                 if max(ext[2] - ext[0], ext[3] - ext[1]) > 200000 else None),
    )


@mcp.tool()
def rwy_review_mapping(path: str, profile: str = "arch",
                       out_dir: str = "") -> Dict[str, Any]:
    """生成“数模对照”确认单：图层 → 构件 + 置信度 + 待确认清单。

    产物：mapping_review.md / .csv / .json / mapping_confirmed.yaml
    这是需求 1 的交付物；必须先看这个再建模。
    """
    try:
        info = scan(path)
        mapper = _mapper(profile)
        _S["drawing"] = info
        wd = Path(out_dir) if out_dir else WORK_DIR / Path(path).stem
        paths = write_review(info, mapper, wd, rules_summary(_rules()))
    except Exception as ex:
        return _err(str(ex))

    results = [mapper.match(l.name) for l in info.layers]
    review = [{"layer": r.layer, "guess": r.component or "未识别",
               "confidence": r.confidence, "entities": l.entity_count}
              for l, r in zip(info.layers, results) if r.action == "manual_review"]
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    return _ok(
        profile=profile, out_dir=str(wd),
        files={k: str(v) for k, v in paths.items()},
        action_summary=counts,
        need_review=review,
        can_auto_build=len(review) == 0,
        next_step=("调用 rwy_confirm_mapping 逐条确认" if review
                   else "可直接 rwy_prepare_build"),
    )


@mcp.tool()
def rwy_confirm_mapping(decisions: Dict[str, str],
                        learn: bool = True) -> Dict[str, Any]:
    """确认/修正图层映射。decisions 形如 {"墙体-240": "wall", "图框": "ignore"}。

    合法值：wall/column/beam/door/window/slab/grid/isolate/ignore
    确认结果写入别名库，同名图层下次自动高分命中。
    """
    valid = {"wall", "column", "beam", "door", "window", "slab", "grid",
             "isolate", "ignore", "manual_review"}
    mapper = _mapper()
    applied, rejected = {}, {}
    for layer, comp in (decisions or {}).items():
        if comp not in valid:
            rejected[layer] = f"非法构件类型 {comp!r}，可选 {sorted(valid)}"
            continue
        mapped = comp if comp in ("isolate", "ignore", "manual_review") else comp
        if learn:
            mapper.learn(layer, mapped)
        applied[layer] = mapped
    if learn and applied:
        save_aliases(mapper.export_aliases(), ALIAS_FILE)
    return _ok(applied=applied, rejected=rejected,
               alias_file=str(ALIAS_FILE), learned=len(applied),
               next_step="rwy_prepare_build")


@mcp.tool()
def rwy_get_rules(custom_rules_path: str = "", as_table: bool = True) -> Dict[str, Any]:
    """读取当前生效的 BIM 建模规则（内置默认 + 用户覆盖），供用户确认或修改。"""
    try:
        rules = _rules(custom_rules_path or None)
    except Exception as ex:
        return _err(str(ex))
    return _ok(
        profile=rules.get("profile"),
        revit_version=_target_revit(rules),
        summary=rules_summary(rules) if as_table else None,
        full=rules if not as_table else None,
        editable_copy_hint=("复制 config/bim_rules/default.yaml 到 "
                            f"{cfg.USER_CONFIG_DIR / 'rules' / 'default.yaml'} 即可覆盖"),
    )


@mcp.tool()
def rwy_set_rules(overrides: Dict[str, Any], merge_with_default: bool = True) -> Dict[str, Any]:
    """临时覆盖建模规则中的若干键，如 {"wall":{"fallback_thickness_mm":240}}。

    深合并到当前规则；回填到本次会话，不落盘（除非调 rwy_save_rules）。
    """
    try:
        base = _rules() if merge_with_default else {}
        rules = cfg.deep_merge(base, overrides)
    except Exception as ex:
        return _err(str(ex))
    _S["rules"] = rules
    return _ok(summary=rules_summary(rules),
               note="已应用于本次会话；需要持久化调 rwy_save_rules")


@mcp.tool()
def rwy_save_rules(path: str = "") -> Dict[str, Any]:
    """把当前规则持久化到用户级配置（默认 ~/.revit-wy/rules/default.yaml）。"""
    rules = _rules()
    target = Path(path) if path else cfg.USER_CONFIG_DIR / "rules" / "default.yaml"
    try:
        cfg.save_yaml(rules, target)
    except Exception as ex:
        return _err(str(ex))
    return _ok(saved=str(target))


@mcp.tool()
def rwy_prepare_build(path: str, profile: str = "arch",
                      base_level: str = "",
                      apply_cleanup: bool = True,
                      out_dir: str = "") -> Dict[str, Any]:
    """从 DXF 构建 IR（构件清单），暂不写入 Revit。

    返回构件统计、示例构件、未配对墙线数等质量指标。
    """
    from .dwg import clean as dwg_clean
    try:
        mapper = _mapper(profile)
        wd = Path(out_dir) if out_dir else WORK_DIR / Path(path).stem
        wd.mkdir(parents=True, exist_ok=True)
        src = path
        if apply_cleanup:
            cres = dwg_clean(path, mapper, wd / f"{Path(path).stem}_clean.dxf")
            src = cres.output
        prims = extract(src, mapper)

        # 把图层后缀厚度喂给墙
        lt: Dict[str, float] = {}
        from .dwg import layer_thickness_from_name
        for layer in set(p.layer for p in prims.get("wall", [])):
            r = mapper.match(layer)
            th = layer_thickness_from_name(layer, r.extras)
            if th:
                lt[layer] = th

        model = build_ir(prims, _rules(), profile=profile, source=path,
                         base_level=base_level or "1层", walls_layer_thickness=lt)
        hosted = assign_host_walls(model)
        _S["ir"] = model
        ir_path = model.save(wd / "model.ir.json")
    except Exception as ex:
        return _err(f"{type(ex).__name__}: {ex}")

    d = model.to_dict()
    return _ok(
        ir_file=str(ir_path), counts=model.counts(),
        quality={
            "hosted_openings": hosted,
            "unhosted_openings": len(model.doors) + len(model.windows) - hosted,
            "grids_named": sum(1 for g in model.grids if not g.name.startswith("G")),
            "walls_from_pairing": sum(1 for w in model.walls if w.source == "pair"),
            "walls_from_fallback": sum(1 for w in model.walls if w.source == "fallback"),
            "unpaired_lines": model.meta.get("unpaired_wall_lines", 0),
        },
        samples={
            "wall": d["walls"][:3], "column": d["columns"][:3],
            "door": d["doors"][:2], "grid": d["grids"][:3],
        },
        next_step="rwy_build 写入 Revit（建议先 dry_run=true）",
    )


@mcp.tool()
def rwy_dwg_to_model(path: str, profile: str = "arch",
                     out_dir: str = "",
                     max_manual_review: int = 0,
                     base_level: str = "") -> Dict[str, Any]:
    """【一步到位】DWG（或 DXF）→ 构件台账，全程自动，只在必须人决策时停下。

    这是「DWG 优先翻模」的主入口。内部固定五步：转换 → 体检 → 数模对照 →
    清洗 → 建 IR，并返回**每一阶段**的结果。相比让 AI 依次调用
    rwy_dwg2dxf / rwy_scan_drawing / rwy_review_mapping / rwy_prepare_build，
    少 3 次编排机会，出错率显著下降（见 docs/08 的 L2 断崖）。

    只在两种情况下停下并把问题交给调用方：
      · 有图层无法自动归类（默认 max_manual_review=0，宁停不乱建）
      · 清洗后提取不到任何构件

    成功后 IR 已存入会话，可直接 rwy_build(dry_run=true) 看将建什么。
    """
    from .dwg_pipeline import run_dwg
    try:
        res = run_dwg(path, profile=profile, out_dir=out_dir,
                      mapper=_mapper(profile), rules=_rules(),
                      base_level=base_level,
                      max_manual_review=int(max_manual_review))
    except Exception as ex:
        return _err(f"{type(ex).__name__}: {ex}")

    d = res.as_dict()
    if not res.ok:
        return _err(
            "流水线未走完，需人工介入",
            stopped_at=next((s["name"] for s in d["stages"] if not s["ok"]), "unknown"),
            blockers=res.blockers, next_steps=res.next_steps,
            stages=d["stages"], review=res.review,
        )
    _S["ir"] = res.model
    return _ok(counts=res.counts, stages=d["stages"], review=res.review,
               cleanup=res.cleanup, ir_file=res.ir_path,
               next_steps=res.next_steps)


@mcp.tool()
def rwy_build(dry_run: bool = True, confirm: bool = False,
              steps: Optional[List[str]] = None,
              host: str = "127.0.0.1", port: int = 48880,
              auto_save: bool = True,
              replace_existing: bool = False) -> Dict[str, Any]:
    """把 IR 写入 Revit 2022+（插件按本机版本编译，协议一致）。默认 dry_run=true 只校验不创建。

    真正落模型必须 dry_run=false 且 confirm=true（双重保险，防止 LLM 误触发）。

    `replace_existing`：写入前先删除上一次翻模生成的构件（靠 Comments 里的
    `revit-wy:` 标记精确识别，**不会动手工建的构件**）。
    改图后重翻时必须打开它，否则同名构件会叠加两份。
    默认关闭——删除是比新增危险得多的动作，必须显式要求。
    """
    model = _S.get("ir")
    if model is None:
        return _err("还没有 IR，请先 rwy_prepare_build 或 rwy_dwg_to_model")
    ir = model.to_dict()

    # 干跑也要连一次 Revit：否则"将删除几个"只能瞎写 0（早期版本就是这么错的）
    existing = 0
    reachable = True
    probe_err = ""
    try:
        with RevitBridge(host, int(port)) as b:
            st = b.status()
            existing = int(st.get("revit_wy_tagged", 0) or 0)
    except Exception as ex:
        reachable = False
        probe_err = str(ex)

    would_delete = existing if replace_existing else 0

    if dry_run:
        check = [
            "标高已存在或可创建", "网格名称不与现有冲突",
            "墙类型将按厚度自动匹配/创建",
            "门窗尝试按邻近墙选宿主，失败则跳过并记录",
        ]
        if not reachable:
            check.append(f"⚠ 连不上 Revit（{probe_err}）：无法核对已有构件数")
        if existing and not replace_existing:
            check.append(
                f"⚠ 模型里已有 {existing} 个上次翻模的构件，本次不会删除，"
                "会与它们叠加；要替换请传 replace_existing=true")
        note = ("确认无误后调用 rwy_build(dry_run=false, confirm=true"
                + (", replace_existing=true)" if replace_existing else ")"))
        return _ok(
            dry_run=True, will_create=model.counts(),
            would_delete=would_delete,
            existing_tagged=existing,
            revit_reachable=reachable,
            check=check, note=note,
        )

    if not confirm:
        return _err("安全闸未开：必须同时传 dry_run=false 且 confirm=true")

    deleted = 0
    try:
        with RevitBridge(host, int(port)) as b:
            if replace_existing and existing:
                deleted = int((b.delete_tagged() or {}).get("deleted", 0))
            res = b.build_ir(ir, steps=steps, dry_run=False, auto_save=auto_save)
    except Exception as ex:
        if deleted:
            return _err(f"已删除 {deleted} 个旧构件，但写入失败：{ex}")
        return _err(str(ex))
    return _ok(result=res, deleted=deleted)


@mcp.tool()
def rwy_status(host: str = "127.0.0.1", port: int = 48880) -> Dict[str, Any]:
    """查询 Revit 侧状态：文档、标高、已有构件数、上次翻转结果。"""
    try:
        with RevitBridge(host, int(port)) as b:
            return _ok(**b.status())
    except Exception as ex:
        return _err(str(ex))


@mcp.tool()
def rwy_dwg2dxf(folder: str, out_dir: str = "", recursive: bool = True) -> Dict[str, Any]:
    """调用本机 AutoCAD（accoreconsole）批量 DWG→DXF，并统一另存为 R2018 DXF。

    需在 Windows + 已装 AutoCAD 的环境执行；Linux/无 AutoCAD 时返回指引。
    """
    from .autocad import convert_folder
    try:
        res = convert_folder(folder, out_dir or "", recursive=recursive)
    except Exception as ex:
        return _err(str(ex))
    return _ok(**res)


@mcp.tool()
def rwy_diff(next_ir_path: str) -> Dict[str, Any]:
    """对比当前会话 IR 与另一份 IR，得到增量构件数（改图后增量重建用）。"""
    from .ir import diff
    cur = _S.get("ir")
    if cur is None:
        return _err("当前会话没有 IR")
    try:
        other = json.loads(Path(next_ir_path).read_text(encoding="utf-8"))
    except Exception as ex:
        return _err(str(ex))
    return _ok(diff=diff(cur.to_dict(), other))


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="revit-wy MCP server")
    ap.add_argument("--profile", default="arch", choices=["arch", "struct", "mep"])
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    args = ap.parse_args(argv)
    _S["profile"] = args.profile
    try:
        mcp.run(transport=args.transport)
    except TypeError:
        # 老版本 run() 不带 transport 参数
        mcp.run()


if __name__ == "__main__":
    main()
