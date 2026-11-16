"""几何算法：线段合并、双线配对（判墙厚）、中心线提取、端点吸附。

输入：dwg.extract() 得到的 Primitive（mm）
输出：WallSegment / RectColumn / BeamSegment 等中立几何
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .dwg import Primitive, Point, polyline_area_mm2

Tol = 1e-6


# ------------------------------------------------------------------ 基础
def dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def angle_of(a: Point, b: Point) -> float:
    """度，0-180（方向无关）。"""
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    return ang


def angle_diff(a1: float, a2: float) -> float:
    d = abs(a1 - a2) % 180.0
    return min(d, 180.0 - d)


def project_point_on_line(p: Point, a: Point, b: Point) -> Tuple[Point, float, float]:
    """返回 (投影点, 距起点的弧长 t, 到直线距离)。"""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < Tol:
        return a, 0.0, dist(p, a)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    proj = (ax + t * dx, ay + t * dy)
    return proj, t * math.sqrt(L2), dist(p, proj)


def point_line_distance(p: Point, a: Point, b: Point) -> float:
    return project_point_on_line(p, a, b)[2]


def line_length(a: Point, b: Point) -> float:
    return dist(a, b)


def segments_from_primitive(p: Primitive) -> List[Tuple[Point, Point]]:
    """把 line / polyline / arc 展开为直线段序列。"""
    if p.kind == "line" and len(p.points) >= 2:
        return [(p.points[0], p.points[1])]
    if p.kind == "polyline":
        pts = p.points
        segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        if p.closed and len(pts) > 2:
            segs.append((pts[-1], pts[0]))
        return segs
    if p.kind == "arc":
        return arc_to_segments(p)
    return []


def arc_to_segments(p: Primitive, segments: int = 24) -> List[Tuple[Point, Point]]:
    cx, cy = p.points[0]
    r = p.radius
    a0, a1 = p.start_angle, p.end_angle
    if a1 <= a0:
        a1 += 360.0
    n = max(4, segments)
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


# ------------------------------------------------------------------ 合并共线
@dataclass
class Seg:
    a: Point
    b: Point
    layer: str = ""
    handle: str = ""

    @property
    def length(self) -> float:
        return dist(self.a, self.b)

    @property
    def angle(self) -> float:
        return angle_of(self.a, self.b)

    def to_dict(self) -> Dict:
        return {"a": list(self.a), "b": list(self.b), "layer": self.layer}


def merge_collinear(
    segs: Sequence[Seg], tol_mm: float = 1.0, max_gap_mm: float = 300.0
) -> List[Seg]:
    """合并同向、共线、端点相邻的线段（图纸断开画法很常见）。"""
    buckets: Dict[int, List[Seg]] = {}
    for s in segs:
        if s.length < 1.0:
            continue
        key = int(round(s.angle))
        buckets.setdefault(key % 180, []).append(s)

    merged: List[Seg] = []
    for _, group in buckets.items():
        group.sort(key=lambda s: -s.length)
        used = [False] * len(group)
        for i, s in enumerate(group):
            if used[i]:
                continue
            cur = Seg(s.a, s.b, s.layer, s.handle)
            changed = True
            while changed:
                changed = False
                for j, t in enumerate(group):
                    if used[j] or j == i:
                        continue
                    if angle_diff(cur.angle, t.angle) > 1.0:
                        continue
                    # t 两端到 cur 所在直线距离都要够近
                    if (point_line_distance(t.a, cur.a, cur.b) > tol_mm or
                            point_line_distance(t.b, cur.a, cur.b) > tol_mm):
                        continue
                    # 找最近端点对
                    pairs = [
                        (0, 0), (0, 1), (1, 0), (1, 1),
                    ]
                    cu = [cur.a, cur.b]
                    tu = [t.a, t.b]
                    best = None
                    for ci, ti in pairs:
                        d = dist(cu[ci], tu[ti])
                        if best is None or d < best[2]:
                            best = (ci, ti, d)
                    if best is None or best[2] > max_gap_mm:
                        # 也允许完全重叠（一条线画了两遍）
                        if not _overlap(cur, t, tol_mm):
                            continue
                        used[j] = True
                        changed = True
                        continue
                    ci, ti, d = best
                    newpt = cu[1 - ci]
                    far = tu[1 - ti]
                    far_proj, _, _ = project_point_on_line(far, cur.a, cur.b)
                    cur = Seg(newpt, far_proj, cur.layer, cur.handle)
                    used[j] = True
                    changed = True
            used[i] = True
            if cur.length >= 1.0:
                merged.append(cur)
    return merged


def _overlap(s: Seg, t: Seg, tol: float) -> bool:
    pa, ta, _ = project_point_on_line(t.a, s.a, s.b)
    pb, tb, _ = project_point_on_line(t.b, s.a, s.b)
    L = s.length
    if L < Tol:
        return False
    lo, hi = min(ta, tb), max(ta, tb)
    return lo <= L + tol and hi >= -tol


# ------------------------------------------------------------------ 双线配对
@dataclass
class WallSegment:
    start: Point
    end: Point
    thickness: float
    layer: str = ""
    source: str = "pair"      # pair | suffix | fallback
    confidence: float = 1.0
    height_mm: Optional[float] = None
    handles: List[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return dist(self.start, self.end)

    def to_dict(self) -> Dict:
        return {
            "start": list(self.start), "end": list(self.end),
            "thickness": round(self.thickness, 1),
            "length": round(self.length, 1),
            "layer": self.layer, "source": self.source,
            "confidence": round(self.confidence, 2),
        }


def pair_walls(
    segs: Sequence[Seg],
    min_t: float = 60.0,
    max_t: float = 500.0,
    parallel_tol_deg: float = 2.0,
    overlap_min_ratio: float = 0.35,
    layer_thickness: Optional[Dict[str, float]] = None,
) -> Tuple[List[WallSegment], List[Seg]]:
    """把平行双线两两配对成墙，返回 (墙, 未配对线)。

    配对判据：
      1) 平行（角度差 <= tol）
      2) 投影重叠长度 >= 较短线的 overlap_min_ratio
      3) 垂直距离落在 [min_t, max_t]
    多个候选时取重叠最长、距离最接近常见墙厚（200/100/240/300）的组合。
    """
    layer_thickness = layer_thickness or {}
    cands: List[Tuple[int, int]] = []
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            s, t = segs[i], segs[j]
            if s.length < 1 or t.length < 1:
                continue
            if angle_diff(s.angle, t.angle) > parallel_tol_deg:
                continue
            d = point_line_distance(t.a, s.a, s.b)
            if not (min_t <= d <= max_t):
                continue
            pa, ta, _ = project_point_on_line(t.a, s.a, s.b)
            pb, tb, _ = project_point_on_line(t.b, s.a, s.b)
            lo, hi = max(0.0, min(ta, tb)), min(s.length, max(ta, tb))
            ov = hi - lo
            if ov <= 0 or ov / max(1e-6, min(s.length, t.length)) < overlap_min_ratio:
                continue
            cands.append((i, j))

    # 偏好顺序：重叠长 > 厚度常见 > 距离小
    def score(pair: Tuple[int, int]) -> Tuple[float, float]:
        i, j = pair
        s, t = segs[i], segs[j]
        d = point_line_distance(t.a, s.a, s.b)
        pa, ta, _ = project_point_on_line(t.a, s.a, s.b)
        pb, tb, _ = project_point_on_line(t.b, s.a, s.b)
        ov = min(s.length, max(ta, tb)) - max(0.0, min(ta, tb))
        common = min(abs(d - x) for x in (100, 120, 150, 180, 200, 240, 250, 300, 370))
        return (ov, -common)

    cands.sort(key=score, reverse=True)
    used: set[int] = set()
    walls: List[WallSegment] = []
    for i, j in cands:
        if i in used or j in used:
            continue
        s, t = segs[i], segs[j]
        used.add(i); used.add(j)
        d = point_line_distance(t.a, s.a, s.b)
        # 中心线 = 两条线在 s 方向上的投影中点的连线
        pa, ta, _ = project_point_on_line(t.a, s.a, s.b)
        pb, tb, _ = project_point_on_line(t.b, s.a, s.b)
        lo, hi = max(0.0, min(ta, tb)), min(s.length, max(ta, tb))
        ux = (s.b[0] - s.a[0]) / max(1e-9, s.length)
        uy = (s.b[1] - s.a[1]) / max(1e-9, s.length)
        # 法向（远离 s 指向 t 的一侧）
        nx, ny = -uy, ux
        if (t.a[0] - s.a[0]) * nx + (t.a[1] - s.a[1]) * ny < 0:
            nx, ny = uy, -ux
        offset = d / 2.0
        p1 = (s.a[0] + ux * lo + nx * offset, s.a[1] + uy * lo + ny * offset)
        p2 = (s.a[0] + ux * hi + nx * offset, s.a[1] + uy * hi + ny * offset)
        walls.append(WallSegment(
            start=(round(p1[0], 2), round(p1[1], 2)),
            end=(round(p2[0], 2), round(p2[1], 2)),
            thickness=round(d, 1),
            layer=s.layer,
            source="pair", confidence=0.95,
            handles=[s.handle, t.handle],
        ))

    leftover = [s for k, s in enumerate(segs) if k not in used]
    return walls, leftover


def walls_from_single_lines(
    segs: Sequence[Seg],
    layer_thickness: Dict[str, float],
    fallback_thickness: float = 200.0,
) -> List[WallSegment]:
    """单线画法（或配对失败）时，用图层后缀厚度/默认厚度兜底。"""
    out = []
    for s in segs:
        th = layer_thickness.get(s.layer, fallback_thickness)
        out.append(WallSegment(
            start=s.a, end=s.b, thickness=th,
            layer=s.layer, source="suffix" if s.layer in layer_thickness else "fallback",
            confidence=0.6 if s.layer in layer_thickness else 0.35,
            handles=[s.handle],
        ))
    return out


# ------------------------------------------------------------------ 柱
@dataclass
class ColumnGeom:
    center: Point
    width: float
    depth: float
    rotation: float = 0.0
    shape: str = "rect"       # rect | circle
    layer: str = ""
    to_dict_extra: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "center": [round(self.center[0], 1), round(self.center[1], 1)],
            "b": round(self.width, 1), "h": round(self.depth, 1),
            "rotation": round(self.rotation, 2), "shape": self.shape,
            "layer": self.layer,
        }


def columns_from_closed(prims: Iterable[Primitive]) -> List[ColumnGeom]:
    """闭合多段线/圆 → 柱。用最小外接矩形取 b/h 与旋转角。"""
    out: List[ColumnGeom] = []
    for p in prims:
        if p.kind == "circle":
            out.append(ColumnGeom(
                center=p.points[0], width=p.radius * 2, depth=p.radius * 2,
                shape="circle", layer=p.layer,
            ))
            continue
        if p.kind != "polyline" or not p.closed or len(p.points) < 3:
            continue
        pts = list(p.points)
        if polyline_area_mm2(pts) < 100.0:
            continue
        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        # 主轴方向
        best = None
        for i in range(len(pts)):
            a = pts[i]
            b = pts[(i + 1) % len(pts)]
            ang = angle_of(a, b)
            # 投影到该方向，取包围盒
            ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            proj = [(x * ux + y * uy) for x, y in pts]
            perp = [(-x * uy + y * ux) for x, y in pts]
            w = max(proj) - min(proj)
            h = max(perp) - min(perp)
            area_ratio = (w * h) / max(1e-6, polyline_area_mm2(pts))
            key = (round(area_ratio, 3), -min(w, h))
            if best is None or key < best[0]:
                best = (key, w, h, ang)
        _, w, h, ang = best
        if w > 3000 or h > 3000 or min(w, h) < 50:
            continue
        out.append(ColumnGeom(
            center=(round(cx, 2), round(cy, 2)),
            width=round(w, 1), depth=round(h, 1),
            rotation=round(ang, 2), shape="rect", layer=p.layer,
        ))
    return out


# ------------------------------------------------------------------ 梁
def beams_from_segments(
    segs: Sequence[Seg],
    default_b: float = 200.0,
    b_map: Optional[Dict[str, float]] = None,
    axis_tol_deg: float = 15.0,
) -> List[WallSegment]:
    """梁：以中心线 + 宽度表示（复用 WallSegment 结构）。"""
    b_map = b_map or {}
    return [
        WallSegment(
            start=s.a, end=s.b,
            thickness=b_map.get(s.layer, default_b),
            layer=s.layer, source="beam", confidence=0.6,
            handles=[s.handle],
        )
        for s in segs
    ]


# ------------------------------------------------------------------ 轴网
@dataclass
class GridLine:
    name: str
    start: Point
    end: Point
    vertical: bool

    @property
    def id(self) -> str:
        """轴网用「名称」当 id：重算后名称不变，才能回传 Revit 定位。

        其他构件用随机 id 是因为它们的几何每次可能微调；轴网名称是人给的，
        天然稳定，用名称做 id 最贴合实际。
        """
        return f"grid-{self.name}"

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "start": [round(self.start[0], 1), round(self.start[1], 1)],
            "end": [round(self.end[0], 1), round(self.end[1], 1)],
            "vertical": self.vertical,
        }


# 轴号黑名单：构件编号（KZ1/KL2/GZ3…）与图号别误当轴号
MEMBER_LABEL = re.compile(
    r"^(KZ|GZ|KL|LL|QL|XL|TL|YBZ|AZ|QZ|L|B|M|C|FM|MLC|TC)[- ]?\d+$", re.I
)
GRID_LABEL_OK = re.compile(r"^(?:[0-9]{1,3}|[A-Z]{1,2}|[0-9]{1,2}/?[0-9]{0,2}|附加|GB?)$")


def _is_grid_label(t: str) -> bool:
    """轴号通常是纯数字、单/双字母，或「1/1」这类附加轴号。"""
    t = t.strip()
    if not t or len(t) > 6:
        return False
    if MEMBER_LABEL.match(t):
        return False
    if GRID_LABEL_OK.match(t.upper()):
        return True
    # 含中文「轴」或「附」的也接受
    return ("轴" in t) or ("附" in t and len(t) <= 4)


def extract_grids(
    segs: Sequence[Seg],
    texts: Sequence[Tuple[str, Point]],
    extend_mm: float = 500.0,
    axis_tol_deg: float = 3.0,
    max_round_digits: int = 0,
) -> List[GridLine]:
    """轴网：先筛水平/垂直长线（同一条轴上的断线先合并），再用最近文字命名。"""
    axis_segs = [s for s in segs if min(s.angle, 180 - s.angle) <= axis_tol_deg
                 or abs(s.angle - 90) <= axis_tol_deg]
    if not axis_segs:
        return []

    # 按“垂直于自身方向的偏移量”聚类
    groups: Dict[Tuple[bool, float], List[Seg]] = {}
    for s in axis_segs:
        vertical = abs(s.angle - 90) <= axis_tol_deg
        if vertical:
            coord = round((s.a[0] + s.b[0]) / 2, max_round_digits)
        else:
            coord = round((s.a[1] + s.b[1]) / 2, max_round_digits)
        groups.setdefault((vertical, coord), []).append(s)

    grids: List[GridLine] = []
    used_names: set[str] = set()
    for (vertical, coord), ss in sorted(groups.items(), key=lambda t: t[0][1]):
        if vertical:
            ys = [p[1] for s in ss for p in (s.a, s.b)]
            lo, hi = min(ys) - extend_mm, max(ys) + extend_mm
            start, end = (coord, lo), (coord, hi)
        else:
            xs = [p[0] for s in ss for p in (s.a, s.b)]
            lo, hi = min(xs) - extend_mm, max(xs) + extend_mm
            start, end = (lo, coord), (hi, coord)

        # 命名：离端点最近的文字
        name, bestd = "", float("inf")
        anchors = [start, end, ((start[0]+end[0])/2, (start[1]+end[1])/2)]
        for txt, pos in texts:
            t = (txt or "").strip()
            if not _is_grid_label(t):
                continue
            d = min(dist(pos, a) for a in anchors)
            # 轴号离轴线端点更近，加权后优先
            if d < bestd and d < 4000:
                bestd, name = d, t
        if not name:
            name = f"G{len(grids)+1}"
        if name in used_names:
            name = f"{name}{len(grids)+1}"
        used_names.add(name)
        grids.append(GridLine(name=name, start=start, end=end, vertical=vertical))

    # 长度过滤：过短的“轴线”多为标注引线
    if grids:
        lens = [dist(g.start, g.end) for g in grids]
        med = sorted(lens)[len(lens) // 2]
        grids = [g for g in grids if dist(g.start, g.end) >= med * 0.35]
    return grids


# ------------------------------------------------------------------ 吸附
def snap_wall_ends(
    walls: List[WallSegment],
    tolerance_mm: float = 200.0,
) -> int:
    """把相邻墙端点吸附到一起，减少 Revit 里墙不闭合导致的缝隙。"""
    pts: List[Point] = []
    for w in walls:
        pts.extend([w.start, w.end])
    changed = 0
    clusters: List[List[int]] = []
    for i, p in enumerate(pts):
        placed = False
        for c in clusters:
            if dist(pts[c[0]], p) <= tolerance_mm:
                c.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    for c in clusters:
        if len(c) < 2:
            continue
        xs = [pts[i][0] for i in c]
        ys = [pts[i][1] for i in c]
        avg = (round(sum(xs) / len(xs), 2), round(sum(ys) / len(ys), 2))
        for i in c:
            if pts[i] != avg:
                if i % 2 == 0:
                    walls[i // 2].start = avg
                else:
                    walls[i // 2].end = avg
                changed += 1
    return changed


def dedupe_walls(walls: List[WallSegment], eps: float = 2.0) -> List[WallSegment]:
    """去重：起终点互换视为同一条墙。"""
    seen: set = set()
    out: List[WallSegment] = []
    for w in walls:
        key = tuple(sorted([
            (round(w.start[0] / eps), round(w.start[1] / eps)),
            (round(w.end[0] / eps), round(w.end[1] / eps)),
        ]))
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out
