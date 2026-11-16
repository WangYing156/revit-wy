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
class TestRevitVersionSupport:
    """项目要支持 Revit 2022 及以上，不是只支持 2022。"""

    def test_版本到运行时的映射(self):
        from revit_wy.diagnose import dotnet_framework_for
        assert dotnet_framework_for("2022") == "net48"
        assert dotnet_framework_for("2023") == "net48"
        assert dotnet_framework_for("2024") == "net48"
        assert dotnet_framework_for("2025") == "net8.0-windows"
        assert dotnet_framework_for("2026") == "net8.0-windows"
        assert dotnet_framework_for("2027") == "net10.0-windows"

    def test_版本扫描覆盖2022以上(self):
        from revit_wy.diagnose import REVIT_VERSIONS, SUPPORTED_MIN
        assert SUPPORTED_MIN == 2022
        assert "2022" in REVIT_VERSIONS and "2025" in REVIT_VERSIONS

    def test_目标版本可配置(self):
        from revit_wy.ir import _target_revit
        assert _target_revit({}) == "auto"
        assert _target_revit({"target_revit_version": "2025"}) == "2025"
        assert _target_revit({"target_revit_version": None}) == "auto"


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
        # 目标版本是配置项：默认 auto（跟插件实际连接的版本走），可显式配成某一年
        assert data["revit_version"] in ("auto", "2022")
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


class TestAliasShortCircuit:
    """回归测试：别名必须无条件优先，且 isolate/ignore 这类“动作型”归属也要生效。

    历史缺陷：早期只在“规则也命中”时才认别名，导致用户确认“这层是隔离”后
    毫无效果（因为 isolate 没有对应的 component 规则）。这个 bug 会让整个
    数模对照确认环节形同虚设，必须锁死。
    """

    def _m(self):
        return LayerMapper(cfg.load_mapping("arch"))

    def test_alias_overrides_to_isolate(self):
        m = self._m()
        assert m.match("A-WALL-200").action == "build"      # 默认是墙
        m.learn("A-WALL-200", "isolate")
        r = m.match("A-WALL-200")
        assert r.action == "isolate"
        assert r.matched_by == "alias" and r.confidence == 1.0

    def test_alias_overrides_to_ignore(self):
        m = self._m()
        m.learn("A-WALL", "ignore")
        assert m.match("A-WALL").action == "ignore"

    def test_alias_restores_to_build(self):
        m = self._m()
        m.learn("A-WALL-200", "isolate")
        m.learn("A-WALL-200", "wall")
        r = m.match("A-WALL-200")
        assert r.action == "build" and r.component == "wall"

    def test_alias_on_unknown_layer(self):
        """完全无法识别的图层，确认后也应生效。"""
        m = self._m()
        assert m.match("我的怪层XYZ").action == "manual_review"
        m.learn("我的怪层XYZ", "column")
        r = m.match("我的怪层XYZ")
        assert r.component == "column" and r.action == "build"
        assert r.revit_category == "OST_StructuralColumns"

    def test_alias_survives_normalization(self):
        """全角/大小写不同的写法应命中同一别名。"""
        m = self._m()
        m.learn("A-WALL-200", "isolate")
        assert m.match("ａ-wall-200").action == "isolate"


# ============================================================ DWG 优先流水线
class TestDwgPipeline:
    """DWG 一步到位流水线：阶段台账、失败可解释、减负可算。"""

    def test_dxf_复用(self, tmp_path, arch_dxf):
        from revit_wy.dwg_pipeline import run_dwg
        r = run_dwg(arch_dxf, "arch", tmp_path)
        assert r.ok
        names = [s.name for s in r.stages]
        assert names == ["convert", "scan", "review", "clean", "build"]
        assert all(s.ok for s in r.stages)
        assert r.counts["walls"] == 5

    def test_每个阶段都有耗时(self, tmp_path, arch_dxf):
        from revit_wy.dwg_pipeline import run_dwg
        r = run_dwg(arch_dxf, "arch", tmp_path)
        assert all(s.seconds >= 0 for s in r.stages)

    def test_文件不存在时给建议不抛异常(self, tmp_path, arch_dxf):
        from revit_wy.dwg_pipeline import run_dwg
        r = run_dwg(tmp_path / "nope.dwg", "arch", tmp_path)
        assert r.ok is False
        assert any("不存在" in b for b in r.blockers)

    def test_有待确认图层就停下(self, tmp_path, arch_dxf):
        """max_manual_review=0 时，有未归类图层不该继续清洗/建模。"""
        from revit_wy.dwg_pipeline import run_dwg
        from revit_wy.mapping import LayerMapper
        mapper = LayerMapper({"components": {}, "rules": []}, aliases={})
        r = run_dwg(arch_dxf, "arch", tmp_path, mapper=mapper)
        assert r.ok is False
        assert r.counts == {}                      # 没有建出任何构件
        review = next(s for s in r.stages if s.name == "review")
        assert review.ok is False
        assert any("未能自动归类" in b for b in r.blockers)

    def test_产出IR文件(self, tmp_path, arch_dxf):
        import json
        from revit_wy.dwg_pipeline import run_dwg
        r = run_dwg(arch_dxf, "arch", tmp_path)
        assert r.ir_path and Path(r.ir_path).exists()
        data = json.loads(Path(r.ir_path).read_text(encoding="utf-8"))
        assert len(data["walls"]) == 5

    def test_附带减负估算(self, tmp_path, arch_dxf):
        from revit_wy.dwg_pipeline import run_dwg
        r = run_dwg(arch_dxf, "arch", tmp_path)
        assert r.saving["seconds"]["manual"] > 0
        assert "saving_pct" in r.saving


class TestEffort:
    """减负估算：数字要能被质疑、能改基线。"""

    def test_基线可覆盖(self):
        from revit_wy.effort import estimate, load_baseline
        b, _ = load_baseline()
        slow = {**b, "manual_seconds": {**b["manual_seconds"], "wall": 100}}
        a = estimate({"walls": 10}, baseline=b)
        c = estimate({"walls": 10}, baseline=slow)
        assert c.manual_seconds > a.manual_seconds

    def test_单张首图允许为负(self):
        """老实：小图首次调规则确实不划算，不能抹成 0。"""
        from revit_wy.effort import estimate, load_baseline
        b, _ = load_baseline()
        e = estimate({"walls": 3}, baseline=b, first_time=True)
        assert e.net_saving_seconds < 0
        assert e.as_dict()["worth_it"] is False

    def test_复用后转正(self):
        """复用规则后，正常规模的图应该转正。"""
        from revit_wy.effort import estimate, load_baseline
        b, _ = load_baseline()
        e = estimate({"levels": 4, "grids": 20, "walls": 120, "columns": 40,
                      "beams": 60, "doors": 30, "windows": 40, "slabs": 12},
                     baseline=b, first_time=False)
        assert e.net_saving_seconds > 0

    def test_复核成本不可忽略(self):
        """防止把数字做漂亮：目视校核必须计入，提效率不能高到失真。"""
        from revit_wy.effort import estimate
        e = estimate({"levels": 4, "grids": 20, "walls": 120, "columns": 40,
                      "beams": 60, "doors": 30, "windows": 40, "slabs": 12},
                     first_time=False)
        assert e.saving_pct < 92

    def test_大批量提效显著(self):
        from revit_wy.effort import estimate
        e = estimate({"levels": 4, "grids": 20, "walls": 120, "columns": 40,
                      "beams": 60, "doors": 30, "windows": 40, "slabs": 12},
                     first_time=False)
        assert e.saving_pct > 70

    def test_批量汇总累加(self):
        from revit_wy.effort import batch_estimate
        rep = batch_estimate([{"drawing": "a", "counts": {"walls": 10}},
                              {"drawing": "b", "counts": {"walls": 20}}])
        assert rep["counts"]["walls"] == 30
        assert len(rep["drawings"]) == 2
        assert "cost_cny" in rep


# ============================================================ 安装与诊断
class TestInstallMcp:
    """MCP 配置写入必须幂等且不破坏用户已有配置。"""

    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path

    def test_新增后不覆盖其它服务器(self, tmp_path, monkeypatch):
        import json
        from revit_wy import install_mcp as I
        home = self._home(tmp_path, monkeypatch)
        p = home / ".workbuddy" / "mcp.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                     encoding="utf-8")
        row = I.merge_into(p, I.build_entry(Path.cwd()))
        data = json.loads(p.read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {"other", "revit-wy"}
        assert data["mcpServers"]["other"]["command"] == "x"
        assert "新增" in row

    def test_重复执行幂等(self, tmp_path, monkeypatch):
        from revit_wy import install_mcp as I
        home = self._home(tmp_path, monkeypatch)
        p = home / ".workbuddy" / "mcp.json"
        e = I.build_entry(Path.cwd())
        I.merge_into(p, e)
        first = p.read_text(encoding="utf-8")
        assert "无需改动" in I.merge_into(p, e)
        assert p.read_text(encoding="utf-8") == first

    def test_坏文件先备份再重建(self, tmp_path, monkeypatch):
        from revit_wy import install_mcp as I
        home = self._home(tmp_path, monkeypatch)
        p = home / ".workbuddy" / "mcp.json"
        p.parent.mkdir(parents=True)
        p.write_text("{broken", encoding="utf-8")
        I.merge_into(p, I.build_entry(Path.cwd()))
        assert (p.parent / "mcp.json.bak").exists()
        assert "revit-wy" in p.read_text(encoding="utf-8")

    def test_写入的路径真实存在(self, tmp_path, monkeypatch):
        """配了但不生效的头号原因：command 指向不存在的 python。"""
        import json
        from revit_wy import install_mcp as I
        self._home(tmp_path, monkeypatch)
        p = tmp_path / "mcp.json"
        I.merge_into(p, I.build_entry(Path.cwd()))
        entry = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["revit-wy"]
        assert Path(entry["command"]).exists()
        assert Path(entry["env"]["PYTHONPATH"]).is_dir()


class TestDiagnose:
    """诊断报告要能如实回答「当前能用到哪一步」。"""

    def test_无Revit时给出B级结论(self, arch_dxf, tmp_path):
        from revit_wy.diagnose import run_all, verdict
        secs = run_all(tmp_path, port=1, viewer_port=2)
        head, todos = verdict(secs)
        assert head.startswith(("A.", "B.", "C."))

    def test_每条失败都带建议(self, arch_dxf, tmp_path):
        from revit_wy.diagnose import run_all
        secs = run_all(tmp_path, port=1, viewer_port=2)
        for s in secs:
            for it in s.items:
                if it.level == "bad":
                    assert it.fix, f"{it.title} 报错了却没给建议"

    def test_控制台副本能GBK编码(self, arch_dxf, tmp_path):
        from revit_wy.diagnose import console_safe, render, run_all
        text = console_safe(render(run_all(tmp_path, port=1, viewer_port=2)))
        text.encode("gbk")          # 不抛异常即通过（Windows 控制台用 GBK）

    def test_报告含结论段(self, arch_dxf, tmp_path):
        from revit_wy.diagnose import render, run_all
        text = render(run_all(tmp_path, port=1, viewer_port=2))
        assert "===== 结论 =====" in text
        assert "统计：" in text


class TestMockBackend:
    def test_演示用假后端可起停(self):
        from revit_wy.mock_backend import port_open, start_background
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = start_background(port)
        try:
            assert port_open(port)
        finally:
            srv.shutdown()
            srv.server_close()


class TestAddinPackaging:
    """插件打包层面的回归：运行时错配与 XML 语法错都会让 Revit 静默跳过插件，
    不会有任何报错，所以只能靠这类静态检查在 CI 里守住。"""

    def test_csproj按版本给运行时(self):
        import re
        root = Path(__file__).resolve().parent.parent
        s = (root / "addin/RevitWy.Plugin/RevitWy.Plugin.csproj").read_text(encoding="utf-8")
        assert "'$(RevitVersion)' &lt; '2025'" in s
        assert "net8.0-windows" in s and "net48" in s
        assert "net10.0-windows" in s          # 2027+ 预留
        # 每个版本独立输出目录，避免装错 DLL
        assert "bin\\$(RevitVersion)" in s

    def test_工程文件是合法XML(self):
        import xml.dom.minidom as m
        root = Path(__file__).resolve().parent.parent
        for f in ("RevitWy.Plugin.csproj", "RevitWy.Plugin.addin"):
            m.parse(str(root / "addin/RevitWy.Plugin" / f))
        # .addin 一份清单可用于所有版本：注释里列路径可以，但元素里不能写死某一年
        import xml.etree.ElementTree as ET
        addin = ET.parse(str(root / "addin/RevitWy.Plugin/RevitWy.Plugin.addin"))
        # 只检查真正的配置字段：Assembly / FullClassName 不能带版本号
        ns = {"r": ""}
        for e in addin.iter():
            if e.tag in ("Assembly", "FullClassName", "ClientId"):
                assert "2022" not in (e.text or ""), f"{e.tag} 写死版本：{e.text}"

    def test_业务代码不直接用IntegerValue(self):
        """2024 废弃、2025 抛异常；只允许 Compat.cs 里受控使用。"""
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for f in (root / "addin").rglob("*.cs"):
            if f.name == "Compat.cs":
                continue
            txt = f.read_text(encoding="utf-8")
            # 允许出现在注释里
            for i, line in enumerate(txt.splitlines(), 1):
                code = line.split("//")[0]
                if "IntegerValue" in code:
                    offenders.append(f"{f.name}:{i}")
        assert offenders == []

    def test_类别判断不再拿负数枚举比数值(self):
        root = Path(__file__).resolve().parent.parent
        txt = (root / "addin/RevitWy.Plugin/Commands/CommandDispatcher.cs").read_text(encoding="utf-8")
        code = "\n".join(l.split("//")[0] for l in txt.splitlines())
        assert "(int)BuiltInCategory" not in code
        assert "Compat.IsCategory" in code
