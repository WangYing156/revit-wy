"""图纸读取、清洗与几何提取（ezdxf）。

职责边界
--------
* 只做“读 + 清洗 + 几何抽取”，不做 BIM 语义判断（那是 mapping/rules 的事）。
* 输出统一为毫米坐标的 Primitive 列表，供 IR 层组装成 Revit 构件。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.math import Vec2

from .mapping import LayerMapper, MatchResult

# $INSUNITS -> 换算到毫米的系数
INSUNITS_TO_MM = {
    0: 1.0,      # 无单位，按图纸惯例当 mm
    1: 25.4,     # inch
    2: 304.8,    # foot
    4: 1.0,      # mm
    5: 10.0,     # cm
    6: 1000.0,   # m
    8: 0.0254,   # microinch
    9: 25.4e-3,  # mil
    10: 914.4,   # yard
}

Point = Tuple[float, float]


@dataclass
class Primitive:
    """最小几何单元，统一为 mm。"""
    kind: str                    # line | arc | polyline | circle | insert | text
    points: List[Point] = field(default_factory=list)
    radius: float = 0.0
    start_angle: float = 0.0
    end_angle: float = 0.0
    closed: bool = False
    handle: str = ""
    layer: str = ""
    block_name: str = ""
    text: str = ""
    scale: float = 1.0
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LayerStats:
    name: str
    entity_count: int
    kinds: Dict[str, int]
    color: int = 7
    frozen: bool = False
    locked: bool = False
    off: bool = False


@dataclass
class DrawingInfo:
    path: str
    dxf_version: str
    insunits: int
    unit_to_mm: float
    extent: Tuple[float, float, float, float]  # mm
    layers: List[LayerStats]
    total_entities: int
    block_names: List[str]
    texts: List[Tuple[str, Point]]


# ---------------------------------------------------------------- 读取 / 探测
def load_doc(path: str | Path):
    p = Path(path)
    if p.suffix.lower() == ".dwg":
        raise ValueError(
            "ezdxf 不能直接读 DWG。请先用 AutoCAD 的 DWG→DXF 批处理"
            "（revit-wy convert）或在 AutoCAD 中另存为 DXF。"
        )
    return ezdxf.readfile(str(p))


def unit_factor(doc) -> Tuple[int, float]:
    try:
        insunits = int(doc.header.get("$INSUNITS", 4) or 4)
    except Exception:
        insunits = 4
    return insunits, INSUNITS_TO_MM.get(insunits, 1.0)


def _doc_texts(msp, factor: float, limit: int = 20000) -> List[Tuple[str, Point]]:
    out: List[Tuple[str, Point]] = []
    for e in msp:
        try:
            if e.dxftype() in ("TEXT",):
                p = e.dxf.insert
                out.append((e.dxf.text, (p.x * factor, p.y * factor)))
            elif e.dxftype() == "MTEXT":
                p = e.dxf.insert
                out.append((e.plain_text(), (p.x * factor, p.y * factor)))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def scan(path: str | Path) -> DrawingInfo:
    """扫描图纸：单位、图层、实体分布、范围、图块、文字（供轴网/门窗命名）。"""
    doc = load_doc(path)
    msp = doc.modelspace()
    insunits, factor = unit_factor(doc)

    kinds: Dict[str, Dict[str, int]] = {}
    for e in msp:
        lay = e.dxf.layer
        kinds.setdefault(lay, {})
        t = e.dxftype()
        kinds[lay][t] = kinds[lay].get(t, 0) + 1

    layer_tbl = {l.dxf.name: l for l in doc.layers}
    layers: List[LayerStats] = []
    for name, k in kinds.items():
        lyr = layer_tbl.get(name)
        layers.append(
            LayerStats(
                name=name,
                entity_count=sum(k.values()),
                kinds=k,
                color=int(getattr(lyr.dxf, "color", 7)) if lyr else 7,
                frozen=bool(getattr(lyr, "is_frozen", lambda: False)()) if lyr else False,
                locked=bool(getattr(lyr, "is_locked", lambda: False)()) if lyr else False,
                off=bool(getattr(lyr.dxf, "on", True) is False) if lyr else False,
            )
        )
    layers.sort(key=lambda x: -x.entity_count)

    try:
        b = ezbbox.extents(msp, fast=True)
        extent = (
            b.extmin.x * factor, b.extmin.y * factor,
            b.extmax.x * factor, b.extmax.y * factor,
        )
    except Exception:
        extent = (0.0, 0.0, 0.0, 0.0)

    block_names = sorted({blk.name for blk in doc.blocks if not blk.name.startswith("*")})

    return DrawingInfo(
        path=str(path),
        dxf_version=doc.dxfversion,
        insunits=insunits,
        unit_to_mm=factor,
        extent=extent,
        layers=layers,
        total_entities=sum(l.entity_count for l in layers),
        block_names=block_names,
        texts=_doc_texts(msp, factor),
    )


# ---------------------------------------------------------------- 清洗
CLEANUP_PREFIX = "_WY_"


@dataclass
class CleanupResult:
    output: str
    moved_to_isolate: Dict[str, int]
    ignored: Dict[str, int]
    kept: Dict[str, int]
    purged_layers: int
    notes: List[str]


def clean(
    src: str | Path,
    mapper: LayerMapper,
    out_path: str | Path | None = None,
    isolate_non_model: bool = True,
    explode_blocks: bool = False,
) -> CleanupResult:
    """清洗图纸：
    1) 按映射把每层归入 build / isolate / ignore / manual_review；
    2) isolate 的图层加前缀并冻结（保留可追溯，不参与建模）；
    3) ignore 的图层直接删除（图框、DEFPOINTS 等）；
    4) 修正单位、清理空图层、0 层零长度线。
    """
    doc = load_doc(src)
    msp = doc.modelspace()
    src_p = Path(src)
    out_path = Path(out_path) if out_path else src_p.with_name(src_p.stem + "_clean.dxf")

    results = {l.name: mapper.match(l.name) for l in scan(src).layers}
    moved: Dict[str, int] = {}
    ignored: Dict[str, int] = {}
    kept: Dict[str, int] = {}
    notes: List[str] = []

    # 1) 处理要忽略的实体
    doomed = []
    for e in msp:
        r = results.get(e.dxf.layer)
        if r is None:
            continue
        if r.action == "ignore":
            ignored[e.dxf.layer] = ignored.get(e.dxf.layer, 0) + 1
            doomed.append(e)
        elif r.action == "isolate" and isolate_non_model:
            moved[e.dxf.layer] = moved.get(e.dxf.layer, 0) + 1
            e.dxf.layer = CLEANUP_PREFIX + e.dxf.layer
        else:
            kept[e.dxf.layer] = kept.get(e.dxf.layer, 0) + 1

    for e in doomed:
        try:
            msp.delete_entity(e)
        except Exception:
            pass

    # 2) 为新图层建表 + 冻结
    if isolate_non_model:
        for name in moved:
            new = CLEANUP_PREFIX + name
            if new not in doc.layers:
                doc.layers.add(new, color=8)
            doc.layers.get(new).freeze()

    # 3) 删除零长度线与空文字
    removed_degenerate = 0
    for e in list(msp):
        try:
            t = e.dxftype()
            if t == "LINE":
                if e.dxf.start.isclose(e.dxf.end, abs_tol=1e-6):
                    msp.delete_entity(e)
                    removed_degenerate += 1
            elif t in ("TEXT", "MTEXT") and not (e.dxf.text if t == "TEXT" else e.plain_text()).strip():
                msp.delete_entity(e)
                removed_degenerate += 1
        except Exception:
            continue

    if explode_blocks:
        try:
            from ezdxf import explode as ezexplode
            ezexplode.multi_insert(list(msp.query("INSERT")), redraw=False)
            notes.append("已炸开所有块引用（门窗属性将丢失，仅建议在线型块干扰时使用）")
        except Exception as ex:
            notes.append(f"炸块失败: {ex}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out_path))
    notes.append(f"已移除退化实体 {removed_degenerate} 个")
    notes.append(
        f"建模层 {len(kept)} 个 / 隔离层 {len(moved)} 个 / 忽略层 {len(ignored)} 个"
    )

    return CleanupResult(
        output=str(out_path),
        moved_to_isolate=moved,
        ignored=ignored,
        kept=kept,
        purged_layers=len(ignored),
        notes=notes,
    )


# ---------------------------------------------------------------- 几何抽取
def _mm(pt: Vec2, f: float) -> Point:
    return (round(pt.x * f, 3), round(pt.y * f, 3))


def extract(path: str | Path, mapper: LayerMapper) -> Dict[str, List[Primitive]]:
    """按 component 分组抽取几何图元（mm）。"""
    doc = load_doc(path)
    msp = doc.modelspace()
    _, f = unit_factor(doc)

    comp_map: Dict[str, Dict[str, Any]] = {}
    for _, rule in mapper.rules:
        comp_map[rule.get("component")] = rule

    grouped: Dict[str, List[Primitive]] = {}

    def push(comp: str, prim: Primitive):
        grouped.setdefault(comp, []).append(prim)

    for e in msp:
        res = mapper.match(e.dxf.layer)
        if res.action != "build":
            continue
        comp = res.component
        t = e.dxftype()
        try:
            if t == "LINE":
                push(comp, Primitive(
                    kind="line",
                    points=[_mm(e.dxf.start, f), _mm(e.dxf.end, f)],
                    handle=e.dxf.handle, layer=e.dxf.layer,
                ))
            elif t == "ARC":
                push(comp, Primitive(
                    kind="arc",
                    points=[_mm(e.dxf.center, f)],
                    radius=e.dxf.radius * f,
                    start_angle=e.dxf.start_angle, end_angle=e.dxf.end_angle,
                    handle=e.dxf.handle, layer=e.dxf.layer,
                ))
            elif t == "CIRCLE":
                push(comp, Primitive(
                    kind="circle", points=[_mm(e.dxf.center, f)],
                    radius=e.dxf.radius * f, closed=True,
                    handle=e.dxf.handle, layer=e.dxf.layer,
                ))
            elif t in ("LWPOLYLINE", "POLYLINE"):
                pts = [_mm(Vec2(p[0], p[1]), f) for p in e.get_points("xy")]
                closed = bool(e.closed) if hasattr(e, "closed") else bool(e.dxf.flags & 1)
                push(comp, Primitive(
                    kind="polyline", points=pts, closed=closed,
                    handle=e.dxf.handle, layer=e.dxf.layer,
                ))
            elif t == "INSERT":
                ins = e.dxf.insert
                push(comp, Primitive(
                    kind="insert",
                    points=[(ins.x * f, ins.y * f)],
                    block_name=e.dxf.name,
                    scale=float(getattr(e.dxf, "xscale", 1.0) or 1.0) * f,
                    handle=e.dxf.handle, layer=e.dxf.layer,
                    extras={
                        "rotation": float(getattr(e.dxf, "rotation", 0.0) or 0.0),
                        "attribs": {
                            a.dxf.tag: a.dxf.text
                            for a in getattr(e, "attribs", [])
                        },
                    },
                ))
            elif t == "TEXT":
                push(comp, Primitive(
                    kind="text", points=[_mm(e.dxf.insert, f)], text=e.dxf.text,
                    handle=e.dxf.handle, layer=e.dxf.layer,
                ))
            elif t == "MTEXT":
                push(comp, Primitive(
                    kind="text", points=[_mm(e.dxf.insert, f)], text=e.plain_text(),
                    handle=e.dxf.handle, layer=e.dxf.layer,
                ))
        except Exception:
            continue

    return grouped


# ---------------------------------------------------------------- 统计辅助
def layer_thickness_from_name(name: str, rule: Dict[str, Any]) -> Optional[float]:
    pat = rule.get("thickness_suffix")
    if not pat:
        return None
    m = re.search(pat, name)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except Exception:
        return None
    return v if 20 <= v <= 2000 else None


def polyline_area_mm2(pts: List[Point]) -> float:
    """鞋带公式，返回绝对面积。"""
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def primitive_bbox(prims: Iterable[Primitive]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for p in prims:
        for x, y in p.points:
            xs.append(x - p.radius)
            ys.append(y - p.radius)
            xs.append(x + p.radius)
            ys.append(y + p.radius)
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))
