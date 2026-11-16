"""端到端链路测试：扫描 → 映射 → 清洗 → 抽取 → IR。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from revit_wy import config as cfg
from revit_wy.dwg import clean, extract, scan
from revit_wy.geometry import (
    angle_diff, merge_collinear, pair_walls, snap_wall_ends,
)
from revit_wy.ir import assign_host_walls, build as build_ir
from revit_wy.mapping import LayerMapper, core_name, normalize


# ------------------------------------------------------------ 映射引擎
class TestMapping:
    def setup_method(self):
        self.m = LayerMapper(cfg.load_mapping("arch"))

    def test_exact(self):
        r = self.m.match("A-WALL")
        assert r.component == "wall" and r.confidence >= 0.9

    def test_glob_thickness_suffix(self):
        r = self.m.match("A-WALL-200")
        assert r.component == "wall"

    def test_chinese(self):
        assert self.m.match("墙体 200").component == "wall"
        assert self.m.match("剪力墙").component == "wall"

    def test_messy_prefixes(self):
        assert self.m.match("$0$A-WALL").component == "wall"
        assert self.m.match("图层1|柱子").component == "column"
        assert self.m.match(" 门 ").component == "door"

    def test_fullwidth(self):
        # 全角 ＷＡＬＬ 归一化后应命中
        assert self.m.match("ＷＡＬＬ").component == "wall"

    def test_isolate_and_ignore(self):
        assert self.m.match("A-DIM").action == "isolate"
        assert self.m.match("图框").action == "ignore"

    def test_unknown_goes_manual(self):
        r = self.m.match("QQ-随便起的名字-999")
        assert r.action in ("manual_review", "wall", "column")

    def test_alias_learning(self):
        m = LayerMapper(cfg.load_mapping("arch"))
        m.learn("我的墙体层", "wall")
        r = m.match("我的墙体层")
        assert r.component == "wall" and r.matched_by == "alias"
        assert r.confidence == 1.0

    def test_normalize_and_core(self):
        assert normalize("Ａ-WALL-200") == "A-WALL-200"
        assert core_name("A-WALL-200") == "AWALL"


# ------------------------------------------------------------ 几何算法
class TestGeom:
    def test_pair_walls_200(self):
        from revit_wy.geometry import Seg
        segs = [Seg((0, 0), (5000, 0)), Seg((0, 200), (5000, 200))]
        walls, left = pair_walls(segs, min_t=60, max_t=500)
        assert len(walls) == 1 and left == []
        w = walls[0]
        assert abs(w.thickness - 200) < 1
        assert abs(w.start[1] - 100) < 1 and abs(w.end[1] - 100) < 1
        assert abs(w.length - 5000) < 1

    def test_non_parallel_not_paired(self):
        from revit_wy.geometry import Seg
        segs = [Seg((0, 0), (5000, 0)), Seg((0, 0), (0, 5000))]
        walls, left = pair_walls(segs)
        assert walls == [] and len(left) == 2

    def test_too_thick_not_paired(self):
        from revit_wy.geometry import Seg
        segs = [Seg((0, 0), (5000, 0)), Seg((0, 800), (5000, 800))]
        walls, _ = pair_walls(segs, max_t=500)
        assert walls == []

    def test_merge_collinear_gap(self):
        from revit_wy.geometry import Seg
        segs = [Seg((0, 0), (2000, 0)), Seg((2000, 0), (5000, 0)),
                Seg((5100, 0), (8000, 0))]
        merged = merge_collinear(segs, max_gap_mm=300)
        assert len(merged) == 1
        assert abs(merged[0].length - 8000) < 1

    def test_angle_diff_wraps(self):
        assert angle_diff(1, 179) == 2
        assert angle_diff(0, 90) == 90

    def test_snap_wall_ends(self):
        from revit_wy.geometry import WallSegment
        ws = [WallSegment((0, 0), (1000, 0), 200),
              WallSegment((1050, 0), (2000, 0), 200)]
        n = snap_wall_ends(ws, tolerance_mm=200)
        assert n >= 2
        assert ws[0].end == ws[1].start


# ------------------------------------------------------------ 图纸处理
class TestDrawing:
    def test_scan_units(self, arch_dxf):
        info = scan(arch_dxf)
        assert info.unit_to_mm == 1.0
        assert info.total_entities > 20
        assert any(l.name == "A-WALL" for l in info.layers)

    def test_clean_isolates_and_ignores(self, arch_dxf, tmp_path):
        m = LayerMapper(cfg.load_mapping("arch"))
        res = clean(arch_dxf, m, tmp_path / "clean.dxf")
        assert Path(res.output).exists()
        # moved_to_isolate 的 key 是原图层名，改名发生在图纸内部
        assert "FURN" in res.moved_to_isolate, res.moved_to_isolate
        assert "图框" in res.ignored

        info2 = scan(res.output)
        names = {l.name for l in info2.layers}
        assert "图框" not in names
        assert any(n.startswith("_WY_") for n in names)

    def test_extract_groups(self, arch_dxf):
        m = LayerMapper(cfg.load_mapping("arch"))
        prims = extract(arch_dxf, m)
        assert "wall" in prims and len(prims["wall"]) > 8
        assert "door" in prims
        assert any(p.kind == "insert" for p in prims["door"])


# ------------------------------------------------------------ IR
class TestIR:
    def _build(self, path, profile="arch"):
        m = LayerMapper(cfg.load_mapping(profile))
        prims = extract(path, m)
        return build_ir(prims, cfg.load_rules(), profile=profile, source=str(path))

    def test_arch_counts(self, arch_dxf):
        model = self._build(arch_dxf)
        c = model.counts()
        assert c["walls"] >= 4, c
        assert c["doors"] >= 1
        assert c["windows"] >= 1
        assert c["grids"] >= 4

    def test_wall_thickness_from_pairing(self, arch_dxf):
        model = self._build(arch_dxf)
        ths = {round(w.thickness) for w in model.walls}
        assert 200 in ths, ths

    def test_hosting(self, arch_dxf):
        model = self._build(arch_dxf)
        n = assign_host_walls(model)
        assert n >= 1

    def test_struct(self, struct_dxf):
        model = self._build(struct_dxf, "struct")
        c = model.counts()
        assert c["columns"] >= 6, c
        assert c["beams"] >= 2, c
        assert c["slabs"] >= 1, c

    def test_ir_json_serializable(self, arch_dxf, tmp_path):
        model = self._build(arch_dxf)
        p = model.save(tmp_path / "ir.json")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["schema"] == "1.0"
        assert data["revit_version"] == "2022"
        assert len(data["walls"]) == len(model.walls)

    def test_rules_override(self, arch_dxf):
        m = LayerMapper(cfg.load_mapping("arch"))
        prims = extract(arch_dxf, m)
        rules = cfg.load_rules()
        rules = cfg.deep_merge(rules, {"levels": {"story_height_mm": 4200}})
        model = build_ir(prims, rules, source=str(arch_dxf))
        assert all(abs(w.height_mm - 4200) < 1 for w in model.walls)


# ------------------------------------------------------------ 报告
class TestReport:
    def test_review_files(self, arch_dxf, tmp_path):
        from revit_wy.report import read_overrides, write_review
        info = scan(arch_dxf)
        m = LayerMapper(cfg.load_mapping("arch"))
        paths = write_review(info, m, tmp_path)
        for k in ("markdown", "csv", "confirmed", "json"):
            assert Path(paths[k]).exists(), k
        md = Path(paths["markdown"]).read_text(encoding="utf-8")
        assert "数模对照" in md
        assert read_overrides(paths["csv"]) == {}
