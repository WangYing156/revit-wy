"""IR 差异对比与整体平移对齐的回归测试。

背景：这两个功能曾各自出过严重缺陷——
  · 按随机 id 比对 → 永远"全新增"
  · 平移量量化后忘了乘回 snap → 平移量缩小 10 倍
所以这里逐条锁死。
"""
from __future__ import annotations

import copy

import pytest

from revit_wy.diffing import (
    compare, detect_translation, sig_column, sig_wall, translate_ir,
)


def W(x1, y1, x2, y2, th=200, i="w"):
    return {"id": i, "start": [x1, y1], "end": [x2, y2], "thickness_mm": th}


def C(cx, cy, b=400, h=400, rot=0, i="c"):
    return {"id": i, "center": [cx, cy], "b_mm": b, "h_mm": h, "rotation_deg": rot}


class TestSignature:
    def test_reversed_wall_same_sig(self):
        """反向绘制的同一面墙必须判为相同。"""
        assert sig_wall(W(0, 0, 5000, 0)) == sig_wall(W(5000, 0, 0, 0))

    def test_thickness_changes_sig(self):
        assert sig_wall(W(0, 0, 5000, 0, 200)) != sig_wall(W(0, 0, 5000, 0, 240))

    def test_column_rotation_normalized(self):
        """旋转 90° 的矩形柱几何等价，应判相同。"""
        assert sig_column(C(0, 0, rot=0)) == sig_column(C(0, 0, rot=90))

    def test_column_size_differs(self):
        assert sig_column(C(0, 0, 400, 400)) != sig_column(C(0, 0, 500, 500))


class TestCompare:
    def test_identical(self):
        a = {"walls": [W(0, 0, 5000, 0)]}
        r = compare(a, copy.deepcopy(a))
        assert r.summary()["total_unchanged"] == 1
        assert r.summary()["total_added"] == 0

    def test_added(self):
        a = {"walls": [W(0, 0, 5000, 0)]}
        b = {"walls": [W(0, 0, 5000, 0), W(0, 3000, 5000, 3000)]}
        r = compare(a, b)
        assert r.summary()["total_added"] == 1
        assert r.summary()["total_unchanged"] == 1

    def test_thickness_change_is_modified_not_add_remove(self):
        a = {"walls": [W(0, 0, 5000, 0, 200)]}
        b = {"walls": [W(0, 0, 5000, 0, 240)]}
        s = compare(a, b).summary()
        assert s["total_modified"] == 1
        assert s["total_added"] == 0 and s["total_removed"] == 0

    def test_random_ids_do_not_break_match(self):
        """核心回归：两次构建的 id 完全不同，仍应正确识别为"未变化"。"""
        a = {"walls": [{"start": [0, 0], "end": [5000, 0],
                        "thickness_mm": 200, "id": "wall-aaaaaaaaaa"}]}
        b = {"walls": [{"start": [0, 0], "end": [5000, 0],
                        "thickness_mm": 200, "id": "wall-bbbbbbbbbb"}]}
        s = compare(a, b).summary()
        assert s["total_unchanged"] == 1
        assert s["total_added"] == 0


class TestTranslation:
    def _pair(self):
        old = {"walls": [W(0, 0, 5000, 0), W(0, 0, 0, 4000),
                         W(5000, 0, 5000, 4000), W(0, 4000, 5000, 4000)],
               "columns": [C(0, 0), C(5000, 4000)]}
        new = copy.deepcopy(old)
        new = translate_ir(new, 10000.0, 5000.0)
        return old, new

    def test_detect_translation(self):
        old, new = self._pair()
        assert detect_translation(old, new) == (10000.0, 5000.0)

    def test_compare_auto_aligns(self):
        old, new = self._pair()
        r = compare(old, new)
        assert r.aligned == {"dx_mm": 10000.0, "dy_mm": 5000.0}
        s = r.summary()
        # 对齐后应全部判为未变化，而不是"全删全增"
        assert s["total_added"] == 0 and s["total_removed"] == 0
        assert s["total_unchanged"] == 6

    def test_no_translation_detected_when_disabled(self):
        old, new = self._pair()
        r = compare(old, new, auto_align=False)
        assert r.aligned is None
        s = r.summary()
        assert s["total_added"] == 6 and s["total_removed"] == 6

    def test_translation_plus_local_edit(self):
        """整体平移 + 局部改厚 + 新增，三者共存仍应算准。"""
        old, new = self._pair()
        new["walls"][0]["thickness_mm"] = 240
        new["walls"].append(W(10000, -2000, 15000, -2000))
        r = compare(old, new)
        assert r.aligned["dx_mm"] == 10000.0
        s = r.summary()
        assert s["total_added"] == 1
        assert s["total_modified"] == 1

    def test_small_offset_not_aligned(self):
        """小于 snap 的偏移不该触发对齐（避免误判）。"""
        old = {"walls": [W(0, 0, 5000, 0)]}
        new = {"walls": [W(5, 3, 5005, 3)]}
        assert detect_translation(old, new) is None

    def test_translate_ir_does_not_mutate_input(self):
        ir = {"walls": [W(0, 0, 5000, 0)]}
        out = translate_ir(ir, 100, 100)
        assert ir["walls"][0]["start"] == [0, 0]
        assert out["walls"][0]["start"] == [100.0, 100.0]
