"""IR 增量对比：按「几何签名」而非 id 比对。

为什么不能直接用 id
------------------
IR 里的 id 是每次运行随机生成的（`wall-3f2a1c9e04`），两次构建的 id 一定不同，
直接比 id 会得到"全部新增、全部删除"的假结果——这个坑必须绕开。

几何签名（stable key）
---------------------
用构件的**几何不变量**做键：
  * 墙/梁：两端点四舍五入到 1mm 后排序（与方向无关）+ 厚度
  * 柱：中心点 + 截面 + 旋转角归一到 90° 内
  * 门窗：中心点 + 宽度
  * 板：轮廓点的排序哈希

容差按 `tol_mm` 量化，避免浮点尾差导致"看起来一样却判成不同"。
这样改图重跑时能准确分出"新增/删除/移动/改尺寸"四类。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

Point = Tuple[float, float]


def _q(v: float, tol: float) -> int:
    return int(round(v / tol))


def _pt(p, tol: float) -> Tuple[int, int]:
    return (_q(float(p[0]), tol), _q(float(p[1]), tol))


def _hash(*parts) -> str:
    s = "|".join(str(p) for p in parts)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 签名
def sig_wall(e: Dict[str, Any], tol: float = 2.0) -> str:
    a, b = _pt(e["start"], tol), _pt(e["end"], tol)
    lo, hi = sorted((a, b))
    return _hash("wall", lo, hi, _q(e.get("thickness_mm", 0), 5))


def sig_beam(e: Dict[str, Any], tol: float = 2.0) -> str:
    a, b = _pt(e["start"], tol), _pt(e["end"], tol)
    lo, hi = sorted((a, b))
    return _hash("beam", lo, hi, _q(e.get("b_mm", 0), 5))


def sig_column(e: Dict[str, Any], tol: float = 2.0) -> str:
    c = _pt(e["center"], tol)
    b = _q(e.get("b_mm", 0), 5)
    h = _q(e.get("h_mm", 0), 5)
    lo, hi = sorted((b, h))
    # 旋转角归一到 [0,90)，避免同一根柱因方向标注差异被判为不同
    rot = _q(float(e.get("rotation_deg", 0)) % 90.0, 2)
    return _hash("column", c, lo, hi, rot)


def sig_opening(e: Dict[str, Any], tol: float = 2.0) -> str:
    c = _pt(e["center"], tol)
    return _hash(e.get("kind", "door"), c, _q(e.get("width_mm", 0), 5))


def sig_slab(e: Dict[str, Any], tol: float = 2.0) -> str:
    pts = sorted(_pt(p, tol) for p in (e.get("boundary") or []))
    return _hash("slab", pts, _q(e.get("thickness_mm", 0), 5))


SIGNERS = {
    "walls": sig_wall,
    "beams": sig_beam,
    "columns": sig_column,
    "doors": sig_opening,
    "windows": sig_opening,
    "slabs": sig_slab,
}

KEY_LABEL = {
    "walls": "墙", "beams": "梁", "columns": "柱",
    "doors": "门", "windows": "窗", "slabs": "楼板",
}


@dataclass
class ChangeSet:
    added: Dict[str, List[Dict]] = field(default_factory=dict)
    removed: Dict[str, List[Dict]] = field(default_factory=dict)
    unchanged: Dict[str, int] = field(default_factory=dict)
    # 同一位置但尺寸/厚度变化的，单列为 modified
    modified: Dict[str, List[Dict]] = field(default_factory=dict)
    # 检测到的整体平移（None 表示没有平移或已对齐）
    aligned: Optional[Dict[str, float]] = None

    def summary(self) -> Dict[str, Any]:
        return {
            "added": {k: len(v) for k, v in self.added.items()},
            "removed": {k: len(v) for k, v in self.removed.items()},
            "modified": {k: len(v) for k, v in self.modified.items()},
            "unchanged": dict(self.unchanged),
            "total_added": sum(len(v) for v in self.added.values()),
            "total_removed": sum(len(v) for v in self.removed.values()),
            "total_modified": sum(len(v) for v in self.modified.values()),
            "total_unchanged": sum(self.unchanged.values()),
            "aligned": self.aligned,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "unchanged": self.unchanged,
            "aligned": self.aligned,
            "summary": self.summary(),
        }


def _center_of(kind: str, e: Dict[str, Any]) -> Optional[Point]:
    try:
        if kind in ("walls", "beams"):
            return ((e["start"][0] + e["end"][0]) / 2, (e["start"][1] + e["end"][1]) / 2)
        if kind in ("columns", "doors", "windows"):
            return (e["center"][0], e["center"][1])
        if kind == "slabs":
            pts = e.get("boundary") or []
            if not pts:
                return None
            return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    except (KeyError, IndexError, TypeError):
        return None
    return None


# ------------------------------------------------------------------ 整体平移检测
def _shape_key(kind: str, e: Dict[str, Any], tol: float = 5.0):
    """只描述"形状"，不含绝对位置。用于跨位置配对。

    整体平移会破坏所有位置签名，但形状不变——所以先用形状配对，
    再统计配对得到的位移向量，取众数即为全局平移量。
    """
    try:
        if kind in ("walls", "beams"):
            ax, ay = float(e["start"][0]), float(e["start"][1])
            bx, by = float(e["end"][0]), float(e["end"][1])
            length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            # 角度归一到 [0,180)，方向无关
            ang = math.degrees(math.atan2(by - ay, bx - ax)) % 180.0
            th = e.get("thickness_mm", e.get("b_mm", 0))
            return ("L", _q(length, tol), _q(ang, 1.0), _q(th, 5))
        if kind == "columns":
            return ("C", _q(e.get("b_mm", 0), 5), _q(e.get("h_mm", 0), 5),
                    _q(float(e.get("rotation_deg", 0)) % 90.0, 2))
        if kind in ("doors", "windows"):
            return ("O", e.get("kind", "door"), _q(e.get("width_mm", 0), 5),
                    _q(e.get("height_mm", 0), 5))
        if kind == "slabs":
            pts = [(_q(float(p[0]), tol), _q(float(p[1]), tol))
                   for p in (e.get("boundary") or [])]
            if len(pts) >= 2:
                # 用轮廓相对首点的形状向量 + 边数描述
                x0, y0 = pts[0]
                rel = sorted((x - x0, y - y0) for x, y in pts)
                return ("S", len(pts), _hash(rel), _q(e.get("thickness_mm", 0), 5))
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def detect_translation(old: Dict[str, Any], new: Dict[str, Any],
                       tol_mm: float = 5.0, snap_mm: float = 10.0,
                       max_pairs_per_group: int = 400,
                       min_votes_ratio: float = 0.25) -> Optional[Tuple[float, float]]:
    """检测两份 IR 之间的整体平移量 (dx, dy)。

    算法（投票法）：
      1. 按"形状键"把构件分组（形状键不含绝对位置）
      2. 同组的旧/新构件两两配对，算位移向量，量化到 snap_mm 后计票
      3. 得票最多的偏移即为全局平移；票数占比不足则判为"不是整体平移"

    为什么用投票而不是直接比 bbox/质心：
      改图时可能同时新增/删除了构件，质心会被拉偏；投票只看"多数构件的
      一致位移"，对增减构件免疫。
    """
    votes: Dict[Tuple[int, int], int] = {}
    total_pairs = 0

    for kind in SIGNERS:
        old_items = old.get(kind) or []
        new_items = new.get(kind) or []
        if not old_items or not new_items:
            continue

        groups_old: Dict[Any, List[Dict]] = {}
        for e in old_items:
            k = _shape_key(kind, e, tol_mm)
            if k is not None:
                groups_old.setdefault(k, []).append(e)
        groups_new: Dict[Any, List[Dict]] = {}
        for e in new_items:
            k = _shape_key(kind, e, tol_mm)
            if k is not None:
                groups_new.setdefault(k, []).append(e)

        for key, olds in groups_old.items():
            news = groups_new.get(key)
            if not news:
                continue
            # 限制配对数量，避免 O(n²) 爆掉（同形状构件通常不多）
            for oe in olds[:max_pairs_per_group]:
                oc = _center_of(kind, oe)
                if oc is None:
                    continue
                for ne in news[:max_pairs_per_group]:
                    nc = _center_of(kind, ne)
                    if nc is None:
                        continue
                    dx = ne_round(nc[0] - oc[0], snap_mm)
                    dy = ne_round(nc[1] - oc[1], snap_mm)
                    votes[(dx, dy)] = votes.get((dx, dy), 0) + 1
                    total_pairs += 1

    if total_pairs == 0 or not votes:
        return None

    (bx, by), count = max(votes.items(), key=lambda kv: kv[1])
    ratio = count / max(1, total_pairs)
    if ratio < min_votes_ratio:
        return None
    # 票键是"量化后的整数格号"，必须乘回 snap_mm 才是真实位移（mm）
    real_dx, real_dy = bx * snap_mm, by * snap_mm
    if abs(real_dx) < snap_mm and abs(real_dy) < snap_mm:
        return None          # 平移量可忽略
    return (float(real_dx), float(real_dy))


def ne_round(v: float, snap: float) -> int:
    """四舍五入到 snap 的整数倍（返回整数便于做字典键）。"""
    return int(round(v / snap))


def translate_ir(ir: Dict[str, Any], dx: float, dy: float) -> Dict[str, Any]:
    """返回平移后的 IR 副本（不修改入参）。"""
    import copy
    out = copy.deepcopy(ir)

    def mv_pt(p):
        return [float(p[0]) + dx, float(p[1]) + dy]

    for w in out.get("walls") or []:
        w["start"], w["end"] = mv_pt(w["start"]), mv_pt(w["end"])
    for b in out.get("beams") or []:
        b["start"], b["end"] = mv_pt(b["start"]), mv_pt(b["end"])
    for c in out.get("columns") or []:
        c["center"] = mv_pt(c["center"])
    for k in ("doors", "windows"):
        for o in out.get(k) or []:
            o["center"] = mv_pt(o["center"])
    for s in out.get("slabs") or []:
        s["boundary"] = [mv_pt(p) for p in s.get("boundary") or []]
    for g in out.get("grids") or []:
        g["start"], g["end"] = mv_pt(g["start"]), mv_pt(g["end"])
    return out


def compare(old: Dict[str, Any], new: Dict[str, Any],
            tol_mm: float = 2.0, move_tol_mm: float = 300.0,
            auto_align: bool = True) -> ChangeSet:
    """比对两份 IR。

    move_tol_mm：同一类构件在同一位置附近（默认 300mm）但签名不同，视为"改了尺寸/角度"，
    而不是"删一个加一个"。这样报错更贴近人的直觉。

    auto_align：检测到整体平移（如坐标系变更、图纸重新定位）时自动对齐后再比对。
    不做这一步的话，"整张图平移 1000mm" 会被报成"全部删除 + 全部新增"，
    数字巨大且完全误导。
    """
    cs = ChangeSet()
    if auto_align:
        off = detect_translation(old, new, tol_mm=max(tol_mm, 5.0))
        if off is not None:
            cs.aligned = {"dx_mm": off[0], "dy_mm": off[1]}
            old = translate_ir(old, off[0], off[1])
    for kind, signer in SIGNERS.items():
        a_list = old.get(kind) or []
        b_list = new.get(kind) or []

        a_map: Dict[str, List[Dict]] = {}
        for e in a_list:
            a_map.setdefault(signer(e, tol_mm), []).append(e)
        b_map: Dict[str, List[Dict]] = {}
        for e in b_list:
            b_map.setdefault(signer(e, tol_mm), []).append(e)

        added, removed, unchanged = [], [], 0
        for sig, es in b_map.items():
            if sig in a_map:
                unchanged += min(len(es), len(a_map[sig]))
                # 数量不等时，多出来的算新增
                if len(es) > len(a_map[sig]):
                    added.extend(es[len(a_map[sig]):])
            else:
                added.extend(es)
        for sig, es in a_map.items():
            if sig in b_map:
                if len(es) > len(b_map[sig]):
                    removed.extend(es[len(b_map[sig]):])
            else:
                removed.extend(es)

        # 把"位置对得上但签名不同"的配成 modified
        modified: List[Dict] = []
        if added and removed:
            paired_add, paired_rem = set(), set()
            rem_centers = [(_center_of(kind, e), i) for i, e in enumerate(removed)]
            for i, ae in enumerate(added):
                ac = _center_of(kind, ae)
                if ac is None:
                    continue
                best_j, best_d = None, None
                for rc, j in rem_centers:
                    if j in paired_rem or rc is None:
                        continue
                    d = ((ac[0] - rc[0]) ** 2 + (ac[1] - rc[1]) ** 2) ** 0.5
                    if d <= move_tol_mm and (best_d is None or d < best_d):
                        best_j, best_d = j, d
                if best_j is not None:
                    paired_add.add(i)
                    paired_rem.add(best_j)
                    modified.append({
                        "before": removed[best_j],
                        "after": ae,
                        "moved_mm": round(best_d or 0, 1),
                    })
            if paired_add:
                added = [e for i, e in enumerate(added) if i not in paired_add]
            if paired_rem:
                removed = [e for j, e in enumerate(removed) if j not in paired_rem]

        cs.added[kind] = added
        cs.removed[kind] = removed
        cs.modified[kind] = modified
        cs.unchanged[kind] = unchanged
    return cs
