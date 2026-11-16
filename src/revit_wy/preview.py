"""预览数据构建：把 DXF 图元压成适合前端 Canvas 渲染的 JSON。

设计取舍
--------
* 前端要画"原图 + 识别结果叠加"，所以按 **图层** 组织，而不是按构件（dwg.extract 是按构件）。
* 必须限流：一张真实平面图动辄 10 万图元，全量传给浏览器会卡死。
  策略：每层上限 + 全局上限 + 按图元长度优先（长线更能代表结构，短线段多是标注碎线）。
* 坐标保持 mm 原值，缩放交给前端，避免精度丢失。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ezdxf
from ezdxf.math import Vec2

from .dwg import load_doc, unit_factor
from .mapping import LayerMapper

Point = Tuple[float, float]

# 限流参数（可在前端通过查询参数覆盖）
MAX_SEGS_PER_LAYER = 3000
MAX_SEGS_TOTAL = 45000
MAX_POLYLINE_POINTS = 400


@dataclass
class LayerPreview:
    name: str
    component: str
    name_cn: str
    action: str
    confidence: float
    matched_by: str
    reason: str
    revit_category: Optional[str]
    entity_count: int
    segs: List[List[float]]          # [x1,y1,x2,y2]
    circles: List[List[float]]       # [cx,cy,r]
    inserts: List[List[float]]       # [x,y]
    truncated: bool = False


def _arc_segments(cx: float, cy: float, r: float, a0: float, a1: float,
                  n: int = 12) -> List[List[float]]:
    if a1 <= a0:
        a1 += 360.0
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [[pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]]
            for i in range(len(pts) - 1)]


def build_preview(path: str, mapper: LayerMapper,
                  max_per_layer: int = MAX_SEGS_PER_LAYER) -> Dict[str, Any]:
    """返回前端渲染所需的完整数据。"""
    doc = load_doc(path)
    msp = doc.modelspace()
    _, f = unit_factor(doc)

    buckets: Dict[str, Dict[str, Any]] = {}

    def bucket(layer: str) -> Dict[str, Any]:
        b = buckets.get(layer)
        if b is None:
            res = mapper.match(layer)
            b = {
                "name": layer,
                "component": res.component,
                "name_cn": res.name_cn,
                "action": res.action,
                "confidence": res.confidence,
                "matched_by": res.matched_by,
                "reason": res.reason,
                "revit_category": res.revit_category,
                "entity_count": 0,
                "segs": [],
                "circles": [],
                "inserts": [],
                "truncated": False,
            }
            buckets[layer] = b
        return b

    total = 0
    for e in msp:
        if total >= MAX_SEGS_TOTAL:
            break
        layer = e.dxf.layer
        b = bucket(layer)
        b["entity_count"] += 1
        if len(b["segs"]) >= max_per_layer:
            b["truncated"] = True
            continue
        t = e.dxftype()
        try:
            if t == "LINE":
                p1, p2 = e.dxf.start, e.dxf.end
                b["segs"].append([p1.x * f, p1.y * f, p2.x * f, p2.y * f])
                total += 1
            elif t == "LWPOLYLINE":
                pts = [(p[0] * f, p[1] * f) for p in e.get_points("xy")][:MAX_POLYLINE_POINTS]
                if e.closed and len(pts) > 2:
                    pts.append(pts[0])
                for i in range(len(pts) - 1):
                    b["segs"].append([pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]])
                    total += 1
                    if total >= MAX_SEGS_TOTAL:
                        break
            elif t == "POLYLINE":
                pts = [(p[0] * f, p[1] * f) for p in e.get_points("xy")][:MAX_POLYLINE_POINTS]
                for i in range(len(pts) - 1):
                    b["segs"].append([pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]])
                    total += 1
            elif t == "ARC":
                c, r = e.dxf.center, e.dxf.radius * f
                b["segs"].extend(_arc_segments(c.x * f, c.y * f, r,
                                               e.dxf.start_angle, e.dxf.end_angle))
                total += 1
            elif t == "CIRCLE":
                c = e.dxf.center
                b["circles"].append([c.x * f, c.y * f, e.dxf.radius * f])
                total += 1
            elif t == "INSERT":
                p = e.dxf.insert
                b["inserts"].append([p.x * f, p.y * f])
                total += 1
            elif t in ("TEXT", "MTEXT"):
                p = e.dxf.insert
                b["inserts"].append([p.x * f, p.y * f])
                total += 1
        except Exception:
            continue

    layers = sorted(buckets.values(), key=lambda x: -x["entity_count"])

    # 全局包围盒
    xs: List[float] = []
    ys: List[float] = []
    for b in layers:
        for s in b["segs"]:
            xs.extend((s[0], s[2]))
            ys.extend((s[1], s[3]))
        for c in b["circles"]:
            xs.extend((c[0] - c[2], c[0] + c[2]))
            ys.extend((c[1] - c[2], c[1] + c[2]))
        for i in b["inserts"]:
            xs.append(i[0])
            ys.append(i[1])
    bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, 0, 0]

    return {
        "path": str(path),
        "units": {"to_mm": f},
        "bbox_mm": [round(v, 2) for v in bbox],
        "segments_drawn": total,
        "truncated": total >= MAX_SEGS_TOTAL,
        "layers": layers,
    }


def preview_ir(model) -> Dict[str, Any]:
    """把 IR 里的构件也转成可画的形式，用于"识别结果"图层叠加。"""
    out: Dict[str, List[Any]] = {
        "walls": [], "columns": [], "beams": [], "slabs": [],
        "grids": [], "openings": [],
    }
    for w in model.walls:
        out["walls"].append([w.start[0], w.start[1], w.end[0], w.end[1], w.thickness])
    for c in model.columns:
        out["columns"].append([c.center[0], c.center[1], c.b, c.h, c.rotation])
    for b in model.beams:
        out["beams"].append([b.start[0], b.start[1], b.end[0], b.end[1], b.b])
    for s in model.slabs:
        out["slabs"].append([[round(x, 1), round(y, 1)] for x, y in s.boundary])
    for g in model.grids:
        out["grids"].append([g.start[0], g.start[1], g.end[0], g.end[1], g.name])
    for o in list(model.doors) + list(model.windows):
        out["openings"].append([o.center[0], o.center[1], o.width, o.kind])
    return out
