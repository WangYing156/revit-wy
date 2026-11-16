"""DWG 优先的一键翻模流水线：一个入口，五个阶段，全程可回溯。

为什么单独抽一个模块
--------------------
原来链路上的五步（转换 / 体检 / 对照 / 清洗 / 建 IR）分散在 `autocad.py`、
`dwg.py`、`report.py`、`ir.py` 里，命令行能一步步跑，但：

  · 现场工程师不想记 5 条命令 —— 要"给个 DWG 还我一个模型"；
  · MCP 客户端（Cursor / WorkBuddy）每多一步就多一次模型编排，出错率翻倍
    （见 docs/08 的 L2 断崖结论）；
  · 出问题时没人知道断在哪一步。

所以这里把五步固化成一条流水线，并强制产出一份**阶段台账**：
哪一步成功、花了多久、产出了什么文件、卡在哪、下一步该干什么。

与 `mcp_server.rwy_prepare_build` 的关系
---------------------------------------
那个工具处理的是"已经是 DXF"的情形。本模块是它的上游：DWG → DXF 的转换
和转换失败后的补救建议都在这里，两者可以串起来用，也可以单独用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as cfg
from .dwg import clean, extract, layer_thickness_from_name, scan
from .ir import build as build_ir
from .mapping import LayerMapper
from .report import rules_summary, write_review

# 阶段名固定，方便前端和报告按名字取
STAGES = ["convert", "scan", "review", "clean", "build"]


@dataclass
class Stage:
    name: str
    ok: bool = False
    seconds: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = {"name": self.name, "ok": self.ok, "s": round(self.seconds, 2)}
        if self.detail:
            d["detail"] = self.detail
        if self.note:
            d["note"] = self.note
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class DwgResult:
    ok: bool = False
    source: str = ""
    dxf: str = ""
    profile: str = "arch"
    stages: List[Stage] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    review: Dict[str, str] = field(default_factory=dict)
    cleanup: Dict[str, Any] = field(default_factory=dict)
    blockers: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    ir_path: str = ""
    saving: Dict[str, Any] = field(default_factory=dict)
    model: Any = None          # IRModel，不序列化

    def as_dict(self, with_model: bool = False) -> Dict[str, Any]:
        d = {
            "ok": self.ok,
            "source": self.source,
            "dxf": self.dxf,
            "profile": self.profile,
            "counts": self.counts,
            "review": self.review,
            "cleanup": self.cleanup,
            "stages": [s.as_dict() for s in self.stages],
            "blockers": self.blockers,
            "next_steps": self.next_steps,
            "ir_path": self.ir_path,
            "saving": self.saving,
        }
        if with_model:
            d["ir"] = self.model.to_dict() if self.model is not None else None
        return d


# ------------------------------------------------------------------ 工具函数
def _stage(res: DwgResult, name: str) -> Stage:
    st = Stage(name=name)
    res.stages.append(st)
    return st


def _sibling_dxf(dwg: Path) -> Optional[Path]:
    """DWG 旁边如果已经有一份同名 DXF，直接用它 —— 免去重复转换。"""
    cand = dwg.with_suffix(".dxf")
    return cand if cand.exists() and cand.stat().st_size > 1024 else None


def _to_dxf(dwg: Path, work: Path, accoreconsole: str = "",
            allow_oda: bool = True) -> tuple[Optional[Path], str, List[str]]:
    """返回 (dxf, 说明, 补救建议)。三层兜底：accoreconsole → ODA → 同名 DXF。"""
    from .autocad import (convert_folder, convert_folder_oda,
                          find_accoreconsole, find_oda)

    if sibling := _sibling_dxf(dwg):
        return sibling, f"复用同名 DXF：{sibling.name}", []

    accore = find_accoreconsole(accoreconsole)
    if accore:
        r = convert_folder(dwg.parent, work, recursive=False, workers=1,
                           accoreconsole=accore)
        hit = next((c for c in r.get("converted", [])
                    if Path(c["dwg"]).name == dwg.name and c["ok"]), None)
        if hit:
            return Path(hit["dxf"]), "accoreconsole 转换成功", []
        err = next((c.get("error") for c in r.get("converted", [])
                    if Path(c["dwg"]).name == dwg.name), "")
        tips = [
            "确认该 DWG 没有被 AutoCAD 以旧版本独占打开（先关闭）",
            "图纸若有外部参照缺失，accoreconsole 可能静默失败：先在 AutoCAD 里绑定 XREF 再转",
            f"手动转换：在 AutoCAD 中打开后另存为 DXF 2018，放到 {dwg.with_suffix('.dxf').name} 同目录",
        ]
        return None, f"accoreconsole 未产出 DXF：{(err or '')[:200]}", tips

    if allow_oda and find_oda():
        r = convert_folder_oda(dwg.parent, work, recursive=False)
        cand = work / (dwg.stem + ".dxf")
        if cand.exists():
            return cand, "ODA File Converter 转换成功", []
        return None, f"ODA 转换失败：{r}", ["检查 ODAFileConverter 版本与路径"]

    return None, "本机既没有 accoreconsole 也没有 ODA FileConverter", [
        "装 AutoCAD 时自带 accoreconsole.exe（无需额外授权）",
        "或安装免费的 ODA File Converter：https://www.opendesign.com/guestfiles/oda_file_converter",
        "或让上游直接提供 DXF 2018（最省事，ezdxf 可直接读）",
    ]


ALIAS_FILE = cfg.USER_CONFIG_DIR / "aliases.json"


def _default_mapper(profile: str) -> LayerMapper:
    from .mapping import load_aliases
    return LayerMapper(mapping=cfg.load_mapping(profile),
                       aliases=load_aliases(ALIAS_FILE))


# ------------------------------------------------------------------ 主流程
def run_dwg(
    dwg: str | Path,
    profile: str = "arch",
    out_dir: str | Path = "",
    mapper: Optional[LayerMapper] = None,
    rules: Optional[Dict[str, Any]] = None,
    accoreconsole: str = "",
    allow_oda: bool = True,
    base_level: str = "",
    max_manual_review: int = 0,
    write_ir: bool = True,
) -> DwgResult:
    """DWG → IR。失败不抛异常，返回带 blockers 的报告（现场工程师看得懂的那种）。

    `max_manual_review`：允许的"待确认图层"数量上限。默认 0，即只要还有
    图层没归好类就停下等人确认 —— 宁可停，不可乱建（建筑错误代价高）。
    """
    res = DwgResult(source=str(dwg), profile=profile)
    dwg_p = Path(dwg)
    if not dwg_p.exists():
        res.blockers.append(f"文件不存在：{dwg_p}")
        return res

    work = Path(out_dir) if out_dir else cfg.USER_CONFIG_DIR / "work" / dwg_p.stem
    work.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 转换 ----------
    st = _stage(res, "convert")
    t0 = time.time()
    dxf, msg, tips = _to_dxf(dwg_p, work / "dxf", accoreconsole, allow_oda)
    st.seconds = time.time() - t0
    st.note = msg
    if dxf is None:
        st.error = msg
        res.blockers.extend(tips)
        res.stages.append(_stage(res, "scan"))       # 占位，前端好画进度
        res.stages[-1].note = "未执行：上游转换失败"
        for n in STAGES[2:]:
            s = _stage(res, n)
            s.note = "未执行：上游转换失败"
        return res
    st.ok = True
    res.dxf = str(dxf)
    st.detail = {"dxf": str(dxf), "size_kb": round(dxf.stat().st_size / 1024, 1)}

    # ---------- 2. 体检 ----------
    st = _stage(res, "scan")
    t0 = time.time()
    info = scan(dxf)
    st.seconds = time.time() - t0
    st.ok = True
    st.detail = {
        "entities": info.total_entities,
        "layers": len(info.layers),
        "unit_to_mm": info.unit_to_mm,
        "extent_mm": [round(v, 1) for v in info.extent],
    }
    if info.unit_to_mm != 1.0:
        st.note = (f"图纸单位为 {info.unit_to_mm}mm，已自动换算"
                   "（R1 风险点：单位错 1000 倍最易出事）")

    # ---------- 3. 数模对照 ----------
    st = _stage(res, "review")
    t0 = time.time()
    mapper = mapper or _default_mapper(profile)
    rules = rules if rules is not None else cfg.load_rules()
    paths = write_review(info, mapper, work / "review",
                         rules_summary(rules), title=f"{dwg_p.stem} 数模对照确认单")
    st.seconds = time.time() - t0
    res.review = {k: str(v) for k, v in paths.items()}
    matches = [mapper.match(l.name) for l in info.layers]
    manual = [m.layer for m in matches if m.action == "manual_review"]
    st.detail = {
        "layers": len(matches),
        "auto": sum(1 for m in matches if m.action == "build"),
        "isolate": sum(1 for m in matches if m.action == "isolate"),
        "ignore": sum(1 for m in matches if m.action == "ignore"),
        "manual_review": len(manual),
    }
    st.ok = len(manual) <= max_manual_review
    if manual:
        st.note = "待确认：" + "、".join(manual[:8]) + ("…" if len(manual) > 8 else "")
        res.blockers.append(f"有 {len(manual)} 个图层未能自动归类，需人工确认")
        res.next_steps.append(
            "打开数模对照确认单核对，或在命令行执行 "
            f"revit-wy confirm --set \"图层=构件\" -p {profile}")
    if not st.ok:
        for n in ("clean", "build"):
            _stage(res, n).note = "未执行：有待确认图层（宁停不乱建）"
        return res

    # ---------- 4. 清洗 ----------
    st = _stage(res, "clean")
    t0 = time.time()
    cres = clean(dxf, mapper, work / "clean.dxf")
    st.seconds = time.time() - t0
    st.ok = True
    st.detail = {
        "out": cres.output,
        "isolated": sum(cres.moved_to_isolate.values()),
        "ignored": sum(cres.ignored.values()),
        "kept": sum(cres.kept.values()),
        "purged_layers": cres.purged_layers,
    }
    if cres.notes:
        st.note = "；".join(cres.notes[:3])
    res.cleanup = st.detail

    # ---------- 5. 建 IR ----------
    st = _stage(res, "build")
    t0 = time.time()
    prims = extract(cres.output, mapper)
    thick_rule = (rules.get("wall") or {}).get("thickness_from_layer_name")
    thickness = {}
    if thick_rule:
        for layer in {p.layer for v in prims.values() for p in v}:
            t = layer_thickness_from_name(layer, rules.get("wall") or {})
            if t:
                thickness[layer] = t
    model = build_ir(prims, rules, profile=profile, source=str(dwg_p),
                     base_level=base_level or "", walls_layer_thickness=thickness)
    st.seconds = time.time() - t0
    st.ok = True
    res.model = model
    res.counts = model.counts()
    st.detail = dict(res.counts)
    if write_ir:
        ir_path = work / f"{dwg_p.stem}.ir.json"
        model.save(ir_path)
        res.ir_path = str(ir_path)
    total = sum(res.counts.values())
    st.note = f"共 {total} 个构件"
    if total == 0:
        st.ok = False
        res.ok = False
        res.blockers.append("清洗后没有提取到任何构件：检查图层是否被整体 isolate/ignore")
        res.next_steps.append("看数模对照确认单里各图层的归属是否符合预期")
        return res

    # 顺手把减负账算出来：现场工程师最关心的就是这个数
    try:
        from .effort import estimate
        res.saving = estimate(
            res.counts, drawing=dwg_p.name,
            manual_review_layers=st.detail.get("manual_review", 0)
            if "manual_review" in st.detail else 0,
        ).as_dict()
    except Exception:
        res.saving = {}

    res.ok = True
    res.next_steps.append(
        "看效果：revit-wy panel " + str(dxf) + "  → 浏览器打开 http://127.0.0.1:8770/")
    res.next_steps.append(
        "落模型：revit-wy build " + str(dxf) + " --dry-run 先干跑；"
        "确认后 --send 写入 Revit 2022+")
    res.next_steps.append(
        "或交给 AI：在 Cursor/WorkBuddy 里说「把 " + dwg_p.name + " 翻成模型，先干跑」")
    return res


def run_folder(
    folder: str | Path,
    profile: str = "arch",
    out_dir: str | Path = "",
    pattern: str = "*.dwg",
    **kw: Any,
) -> Dict[str, Any]:
    """批量：每张图独立跑一遍，一张失败不影响其他。"""
    folder = Path(folder)
    files = sorted(folder.glob(pattern))
    rows = []
    for f in files:
        r = run_dwg(f, profile=profile, out_dir=Path(out_dir) / f.stem if out_dir else "", **kw)
        rows.append(r.as_dict())
    ok = sum(1 for r in rows if r["ok"])
    return {
        "total": len(rows), "ok": ok, "failed": len(rows) - ok,
        "results": rows,
        "summary": {
            "counts": {r["source"]: r["counts"] for r in rows if r["ok"]},
        },
    }
