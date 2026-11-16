"""Revit 中立中间表示（IR）。

为什么需要 IR
-------------
把「图纸理解」与「Revit 生成」解耦：
  * AutoCAD/DXF 侧只产出 IR，可单测、可 diff、可人工审图；
  * Revit 插件只消费 IR，不关心 DXF 细节；
  * 同一份 IR 可回放、可增量对比（改图后只重建差异构件）。

IR 是纯 JSON，字段全部用 mm / 度 / 层名，不含任何 .NET 类型。
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .dwg import Point, Primitive, polyline_area_mm2
from .geometry import (
    ColumnGeom, GridLine, Seg, WallSegment, beams_from_segments,
    columns_from_closed, dedupe_walls, extract_grids, merge_collinear,
    pair_walls, segments_from_primitive, snap_wall_ends,
    walls_from_single_lines,
)

IR_SCHEMA_VERSION = "1.0"


# ------------------------------------------------------------------ 构件
def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Level:
    name: str
    elevation_mm: float
    height_mm: float = 3000.0

    def to_dict(self) -> Dict:
        return {"name": self.name, "elevation_mm": self.elevation_mm,
                "height_mm": self.height_mm}


@dataclass
class WallEl:
    start: Point
    end: Point
    thickness: float
    base_level: str
    height_mm: float
    top_level: str = ""
    type_hint: str = ""
    source: str = "pair"
    confidence: float = 1.0
    layer: str = ""
    id: str = field(default_factory=lambda: _uid("wall"))

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "kind": "wall",
            "start": [round(self.start[0], 2), round(self.start[1], 2)],
            "end": [round(self.end[0], 2), round(self.end[1], 2)],
            "thickness_mm": round(self.thickness, 1),
            "base_level": self.base_level,
            "top_level": self.top_level,
            "height_mm": round(self.height_mm, 1),
            "type_hint": self.type_hint,
            "source": self.source, "layer": self.layer,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class ColumnEl:
    center: Point
    b: float
    h: float
    rotation: float
    base_level: str
    height_mm: float
    top_level: str = ""
    shape: str = "rect"
    layer: str = ""
    id: str = field(default_factory=lambda: _uid("col"))

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "kind": "column",
            "center": [round(self.center[0], 2), round(self.center[1], 2)],
            "b_mm": round(self.b, 1), "h_mm": round(self.h, 1),
            "rotation_deg": round(self.rotation, 2), "shape": self.shape,
            "base_level": self.base_level, "top_level": self.top_level,
            "height_mm": round(self.height_mm, 1),
            "layer": self.layer,
        }


@dataclass
class BeamEl:
    start: Point
    end: Point
    b: float
    h: float
    base_level: str
    layer: str = ""
    id: str = field(default_factory=lambda: _uid("beam"))

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "kind": "beam",
            "start": [round(self.start[0], 2), round(self.start[1], 2)],
            "end": [round(self.end[0], 2), round(self.end[1], 2)],
            "b_mm": round(self.b, 1), "h_mm": round(self.h, 1),
            "base_level": self.base_level, "layer": self.layer,
        }


@dataclass
class OpeningEl:
    """门窗：位置 + 宿主墙 + 尺寸。宿主墙插入时由 Revit 侧按邻近墙自动选。"""
    center: Point
    width: float
    height: float
    family: str
    base_level: str
    sill_mm: float = 0.0
    rotation: float = 0.0
    block_name: str = ""
    host_wall_id: Optional[str] = None
    layer: str = ""
    kind: str = "door"
    id: str = field(default_factory=lambda: _uid("win"))

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "kind": self.kind,
            "center": [round(self.center[0], 2), round(self.center[1], 2)],
            "width_mm": round(self.width, 1), "height_mm": round(self.height, 1),
            "sill_mm": round(self.sill_mm, 1),
            "rotation_deg": round(self.rotation, 2),
            "family_hint": self.family,
            "base_level": self.base_level,
            "block_name": self.block_name,
            "host_wall_id": self.host_wall_id,
            "layer": self.layer,
        }


@dataclass
class SlabEl:
    boundary: List[Point]
    thickness: float
    base_level: str
    layer: str = ""
    id: str = field(default_factory=lambda: _uid("slab"))

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "kind": "slab",
            "boundary": [[round(x, 2), round(y, 2)] for x, y in self.boundary],
            "thickness_mm": round(self.thickness, 1),
            "base_level": self.base_level, "layer": self.layer,
        }


@dataclass
class IRModel:
    source: str = ""
    profile: str = "arch"
    revit_version: str = "2022"
    units: str = "mm"
    levels: List[Level] = field(default_factory=list)
    walls: List[WallEl] = field(default_factory=list)
    columns: List[ColumnEl] = field(default_factory=list)
    beams: List[BeamEl] = field(default_factory=list)
    doors: List[OpeningEl] = field(default_factory=list)
    windows: List[OpeningEl] = field(default_factory=list)
    slabs: List[SlabEl] = field(default_factory=list)
    grids: List[GridLine] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
        return {
            "levels": len(self.levels), "grids": len(self.grids),
            "walls": len(self.walls), "columns": len(self.columns),
            "beams": len(self.beams), "doors": len(self.doors),
            "windows": len(self.windows), "slabs": len(self.slabs),
        }

    def to_dict(self) -> Dict:
        return {
            "schema": IR_SCHEMA_VERSION,
            "generator": f"revit-wy {__version__}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": self.source,
            "profile": self.profile,
            "revit_version": self.revit_version,
            "units": self.units,
            "levels": [l.to_dict() for l in self.levels],
            "grids": [g.to_dict() for g in self.grids],
            "walls": [w.to_dict() for w in self.walls],
            "columns": [c.to_dict() for c in self.columns],
            "beams": [b.to_dict() for b in self.beams],
            "doors": [d.to_dict() for d in self.doors],
            "windows": [w.to_dict() for w in self.windows],
            "slabs": [s.to_dict() for s in self.slabs],
            "meta": self.meta,
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p


# ------------------------------------------------------------------ 构建
def _level_for(elev: float, levels: List[Level], tol: float = 50.0) -> str:
    if not levels:
        return "标高 1"
    best = min(levels, key=lambda l: abs(l.elevation_mm - elev))
    if abs(best.elevation_mm - elev) <= tol:
        return best.name
    return levels[0].name


def estimate_levels(total_height_mm: float, rules: Dict[str, Any]) -> List[Level]:
    lv = rules.get("levels", {})
    story = float(lv.get("story_height_mm", 3000))
    first = float(lv.get("first_story_elevation_mm", 0))
    n = max(1, int(round(total_height_mm / story)) if total_height_mm > 0 else 1)
    pattern = lv.get("name_pattern", "{story}层")
    return [
        Level(name=pattern.format(story=i + 1), elevation_mm=first + i * story,
              height_mm=story)
        for i in range(n)
    ]


def build(
    primitives: Dict[str, List[Primitive]],
    rules: Dict[str, Any],
    profile: str = "arch",
    source: str = "",
    base_level: str = "标高 1",
    walls_layer_thickness: Optional[Dict[str, float]] = None,
    arcs_segments: int = 24,
) -> IRModel:
    """从按 component 分组的图元构建 IR。"""
    model = IRModel(source=source, profile=profile,
                    revit_version=rules.get("target_revit_version", "2022"))
    wall_cfg = rules.get("wall", {})
    col_cfg = rules.get("column", {})
    beam_cfg = rules.get("beam", {})
    door_cfg = rules.get("door", {})
    win_cfg = rules.get("window", {})
    slab_cfg = rules.get("slab", {})
    grid_cfg = rules.get("grid", {})
    gen = rules.get("modeling", {})
    lv_cfg = rules.get("levels", {})
    floors_mm = float(lv_cfg.get("story_height_mm", 3000))

    # ---------- 标高 ----------
    # 平面图本身不带标高信息；至少保证「当前层 + 上一层」存在，
    # 否则 Revit 无法给墙/柱指定底部与顶部约束（这是翻模失败最常见原因）。
    if not model.levels:
        first = float(lv_cfg.get("first_story_elevation_mm", 0))
        pattern = lv_cfg.get("name_pattern", "{story}层")
        model.levels = [
            Level(name=pattern.format(story=1), elevation_mm=first, height_mm=floors_mm),
            Level(name=pattern.format(story=2), elevation_mm=first + floors_mm,
                  height_mm=floors_mm),
        ]
    # 统一标高名：若调用方给的 base_level 不在本次标高清单里，自动回落到首层，
    # 避免 IR 里出现“标高 1”这种项目里不存在的名字（Revit 侧虽会兜底，但会丢失语义）。
    known = {l.name for l in model.levels}
    if base_level not in known:
        base_level = model.levels[0].name
    top_level = model.levels[1].name if len(model.levels) > 1 else base_level

    # ---------- 轴网 ----------
    grid_prim = primitives.get("grid", [])
    grid_segs = [Seg(a, b, p.layer, p.handle)
                 for p in grid_prim for a, b in segments_from_primitive(p)]
    grid_segs = merge_collinear(grid_segs, max_gap_mm=2000)
    grid_texts = [(p.text, p.points[0]) for p in grid_prim if p.kind == "text"]
    model.grids = extract_grids(
        grid_segs, grid_texts,
        extend_mm=float(grid_cfg.get("extend_mm", 500)),
    )

    # ---------- 墙 ----------
    wall_prim = primitives.get("wall", [])
    wsegs: List[Seg] = []
    for p in wall_prim:
        wsegs.extend(Seg(a, b, p.layer, p.handle)
                     for a, b in segments_from_primitive(p))
    wsegs = merge_collinear(wsegs, tol_mm=1.0, max_gap_mm=250.0)

    pair_cfg = {"min_thickness_mm": 60, "max_thickness_mm": 500,
                "parallel_tol_deg": 2.0, "overlap_min_ratio": 0.35}
    # 规则里的 pair 参数从 mapper extras 里来，这里允许 rules 覆盖
    pair_cfg.update(rules.get("_wall_pair", {}) or {})
    paired, leftover = pair_walls(
        wsegs,
        min_t=float(pair_cfg["min_thickness_mm"]),
        max_t=float(pair_cfg["max_thickness_mm"]),
        parallel_tol_deg=float(pair_cfg["parallel_tol_deg"]),
        overlap_min_ratio=float(pair_cfg["overlap_min_ratio"]),
        layer_thickness=walls_layer_thickness,
    )
    singles = walls_from_single_lines(
        leftover,
        walls_layer_thickness or {},
        fallback_thickness=float(wall_cfg.get("fallback_thickness_mm", 200)),
    )
    all_walls = dedupe_walls(paired + singles,
                             eps=float(gen.get("dedupe_epsilon_mm", 2.0)))
    snap_wall_ends(all_walls, tolerance_mm=float(
        wall_cfg.get("centerline", {}).get("gap_tolerance_mm", 300)))

    min_len = float(gen.get("min_element_length_mm", 50))
    round_mm = int(wall_cfg.get("thickness_round_mm", 10) or 10)
    tmpl = wall_cfg.get("type_name_template", "常规 - {thickness}mm")
    for w in all_walls:
        if w.length < min_len:
            continue
        th = max(50.0, round(w.thickness / round_mm) * round_mm)
        model.walls.append(WallEl(
            start=w.start, end=w.end, thickness=th,
            base_level=base_level,
            height_mm=floors_mm,
            top_level=top_level,
            type_hint=tmpl.format(thickness=int(th)),
            source=w.source, confidence=w.confidence, layer=w.layer,
        ))

    # ---------- 柱 ----------
    for c in columns_from_closed(primitives.get("column", [])):
        model.columns.append(ColumnEl(
            center=c.center, b=c.width, h=c.depth, rotation=c.rotation,
            base_level=base_level, height_mm=floors_mm,
            top_level=top_level, shape=c.shape, layer=c.layer,
        ))

    # ---------- 梁 ----------
    beam_prim = primitives.get("beam", [])
    if beam_prim:
        bsegs: List[Seg] = []
        for p in beam_prim:
            bsegs.extend(Seg(a, b, p.layer, p.handle)
                         for a, b in segments_from_primitive(p))
        # 梁常为双线，先尝试配对求宽度，再退化为单线
        bpair, bleft = pair_walls(
            merge_collinear(bsegs, max_gap_mm=300.0),
            min_t=100, max_t=1200, overlap_min_ratio=0.3,
        )
        for w in bpair:
            if w.length < min_len:
                continue
            model.beams.append(BeamEl(
                start=w.start, end=w.end, b=round(w.thickness, 0),
                h=float(beam_cfg.get("default_h", 500)),
                base_level=base_level, layer=w.layer,
            ))
        for s in beams_from_segments(
            merge_collinear(bleft, max_gap_mm=300.0),
            default_b=float(beam_cfg.get("default_b", 200)),
        ):
            if s.length < min_len:
                continue
            model.beams.append(BeamEl(
                start=s.start, end=s.end, b=s.thickness,
                h=float(beam_cfg.get("default_h", 500)),
                base_level=base_level, layer=s.layer,
            ))

    # ---------- 门窗 ----------
    # 图纸常把门窗画在同一图层，先按块名/图层名消歧，再落门或窗
    from .mapping import opening_kind
    opening_prim = list(primitives.get("door", [])) + list(primitives.get("window", []))
    buckets: Dict[str, List[Primitive]] = {"door": [], "window": []}
    for p in opening_prim:
        if p.kind != "insert":
            continue
        kind = opening_kind(p.block_name, p.layer)
        buckets[kind].append(p)
    primitives = {**primitives, "door": buckets["door"], "window": buckets["window"]}

    for kind, cfg in (("door", door_cfg), ("window", win_cfg)):
        for p in primitives.get(kind, []):
            w = float(cfg.get("default_width_mm", 900))
            h = float(cfg.get("default_height_mm", 2100))
            attribs = p.extras.get("attribs", {}) or {}
            for key in ("宽度", "WIDTH", "宽", "B"):
                if key in attribs:
                    try:
                        w = float(str(attribs[key]).strip())
                    except Exception:
                        pass
                    break
            for key in ("高度", "HEIGHT", "高", "H"):
                if key in attribs:
                    try:
                        h = float(str(attribs[key]).strip())
                    except Exception:
                        pass
                    break
            el = OpeningEl(
                center=p.points[0], width=w, height=h,
                family=str(cfg.get("fallback_family", "")),
                base_level=base_level,
                sill_mm=float(cfg.get("sill_height_mm", 0)) if kind == "window" else 0.0,
                rotation=float(p.extras.get("rotation", 0.0)),
                block_name=p.block_name,
                layer=p.layer, kind=kind,
            )
            (model.doors if kind == "door" else model.windows).append(el)

    # ---------- 楼板 ----------
    min_area = float(gen.get("min_area_mm2", 10000))
    for p in primitives.get("slab", []):
        if p.kind != "polyline" or not p.closed or len(p.points) < 3:
            continue
        if polyline_area_mm2(p.points) < min_area:
            continue
        model.slabs.append(SlabEl(
            boundary=list(p.points),
            thickness=float(slab_cfg.get("default_thickness_mm", 120)),
            base_level=base_level, layer=p.layer,
        ))

    model.meta["counts"] = model.counts()
    model.meta["unpaired_wall_lines"] = len(leftover)
    model.meta["rules_profile"] = rules.get("profile", "default_bim_rules")
    return model


def assign_host_walls(model: IRModel, max_distance_mm: float = 400.0) -> int:
    """给门窗找宿主墙：中心点到墙中心线距离最小且落在墙段内。"""
    from .geometry import project_point_on_line
    n = 0
    for op in list(model.doors) + list(model.windows):
        best = None
        for w in model.walls:
            proj, t, d = project_point_on_line(op.center, w.start, w.end)
            L = math.hypot(w.end[0] - w.start[0], w.end[1] - w.start[1])
            if not (-50 <= t <= L + 50):
                continue
            if d > max(w.thickness, max_distance_mm) / 2 + 50:
                continue
            if best is None or d < best[1]:
                best = (w.id, d)
        if best:
            op.host_wall_id = best[0]
            n += 1
    return n


def diff(a: Dict, b: Dict, keys=("walls", "columns", "beams", "doors", "windows", "slabs")):
    """IR 增量对比，用于改图后只重建变更构件。"""
    out: Dict[str, Any] = {}
    for k in keys:
        sa = {tuple(sorted(e.get("id", ""))) for e in a.get(k, [])}
        ida = {e.get("id") for e in a.get(k, [])}
        idb = {e.get("id") for e in b.get(k, [])}
        out[k] = {
            "added": len(idb - ida), "removed": len(ida - idb),
            "same": len(ida & idb),
        }
    return out
