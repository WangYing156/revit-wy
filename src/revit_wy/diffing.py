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
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "unchanged": self.unchanged,
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


def compare(old: Dict[str, Any], new: Dict[str, Any],
            tol_mm: float = 2.0, move_tol_mm: float = 300.0) -> ChangeSet:
    """比对两份 IR。

    move_tol_mm：同一类构件在同一位置附近（默认 300mm）但签名不同，视为"改了尺寸/角度"，
    而不是"删一个加一个"。这样报错更贴近人的直觉。
    """
    cs = ChangeSet()
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
