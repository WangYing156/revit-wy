"""桥接集成测试：真起一个 mock Revit 服务，验证 Python ↔ 插件端协议契约。

这一步的价值：C# 插件无法在 Linux CI 编译，但协议层可以在 CI 里守住。
任何 IR 字段改名/漏字段，这里会立刻失败。
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from revit_wy import config as cfg
from revit_wy.bridge import RevitBridge, probe
from revit_wy.dwg import extract
from revit_wy.ir import assign_host_walls, build as build_ir
from revit_wy.mapping import LayerMapper

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import mock_revit  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_server():
    port = _free_port()
    srv = mock_revit.Server(("127.0.0.1", port), mock_revit.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.15)
    yield port
    srv.shutdown()
    srv.server_close()


def _model(path):
    m = LayerMapper(cfg.load_mapping("arch"))
    prims = extract(path, m)
    model = build_ir(prims, cfg.load_rules(), source=str(path))
    assign_host_walls(model)
    return model


class TestBridge:
    def test_probe_connected(self, mock_server):
        info = probe("127.0.0.1", mock_server)
        assert info["connected"] is True
        assert info["revit_version"] == "2022"
        assert info["protocol"] == "revit-wy/1"

    def test_probe_refused(self):
        info = probe("127.0.0.1", _free_port(), timeout=1.0)
        assert info["connected"] is False

    def test_ping_status(self, mock_server):
        with RevitBridge("127.0.0.1", mock_server) as b:
            assert b.ping()["pong"] is True
            st = b.status()
            assert "counts" in st and "walls" in st["counts"]

    def test_dry_run_build(self, arch_dxf, mock_server):
        model = _model(arch_dxf)
        with RevitBridge("127.0.0.1", mock_server) as b:
            res = b.build_ir(model.to_dict(), dry_run=True)
        assert res["dry_run"] is True
        assert res["created"]["walls"] == 0

    def test_real_build(self, arch_dxf, mock_server):
        model = _model(arch_dxf)
        with RevitBridge("127.0.0.1", mock_server) as b:
            res = b.build_ir(model.to_dict(), dry_run=False, auto_save=False)
        assert res["ok"] is True, res["errors"]
        assert res["errors"] == []
        assert res["created"]["walls"] == len(model.walls) > 0
        assert res["created"]["levels"] >= 2
        assert res["created"]["doors"] >= 1
        assert res["created"]["windows"] >= 1

    def test_unknown_method_errors(self, mock_server):
        with RevitBridge("127.0.0.1", mock_server) as b:
            with pytest.raises(Exception) as ei:
                b.call("no_such_method")
        assert "UNSUPPORTED" in str(ei.value)

    def test_ir_contract_fields(self, arch_dxf):
        """IR 必须带齐插件端反序列化所需字段。"""
        model = _model(arch_dxf)
        d = model.to_dict()
        for k in mock_revit.REQUIRED_IR_FIELDS:
            assert k in d, k
        for key, fields in mock_revit.REQUIRED_ITEM_FIELDS.items():
            for item in d[key]:
                for f in fields:
                    assert f in item, f"{key} 缺 {f}"

    def test_all_levels_resolvable(self, arch_dxf):
        """墙/柱引用的标高名必须真的在 levels 里（否则 Revit 侧会兜底失真）。"""
        model = _model(arch_dxf)
        names = {l.name for l in model.levels}
        for w in model.walls:
            assert w.base_level in names
            assert w.top_level in names


# ============================================================ 重复翻模安全
class TestReplaceExisting:
    """v0.1 的严重缺口：改图后重翻会叠加两份构件。
    这里锁定新契约——干跑必须报出已有构件数，真写必须能精确清理。"""

    def _prep(self, monkeypatch, port, arch_dxf, tmp_path):
        from revit_wy import mcp_server as M
        monkeypatch.setattr(M, "WORK_DIR", tmp_path / "work")
        M._S["ir"] = None
        M._S["mapper"] = None
        self.port = port
        # mock 的状态是模块级共享的，每个用例从干净模型开始，避免互相污染
        import mock_revit as _mr
        _mr.STATE.__init__()
        assert M.rwy_dwg_to_model(str(arch_dxf), "arch")["ok"]

    def test_干跑报已有构件数与可删除数(self, monkeypatch, mock_server, arch_dxf, tmp_path):
        from revit_wy import mcp_server as M
        self._prep(monkeypatch, mock_server, arch_dxf, tmp_path)
        d1 = M.rwy_build(dry_run=True, port=self.port)
        assert d1["ok"] and d1["revit_reachable"] is True
        assert d1["existing_tagged"] == 0
        assert d1["would_delete"] == 0

    def test_写了之后再干跑会警告叠加(self, monkeypatch, mock_server, arch_dxf, tmp_path):
        from revit_wy import mcp_server as M
        self._prep(monkeypatch, mock_server, arch_dxf, tmp_path)
        assert M.rwy_build(dry_run=False, confirm=True, port=self.port)["ok"]
        d2 = M.rwy_build(dry_run=True, port=self.port)
        assert d2["existing_tagged"] > 0
        assert any("叠加" in c for c in d2["check"])

    def test_replace_existing真正清掉旧的(self, monkeypatch, mock_server, arch_dxf, tmp_path):
        from revit_wy import mcp_server as M
        self._prep(monkeypatch, mock_server, arch_dxf, tmp_path)
        assert M.rwy_build(dry_run=False, confirm=True, port=self.port)["ok"]
        n = M.rwy_build(dry_run=True, port=self.port)["existing_tagged"]
        r = M.rwy_build(dry_run=False, confirm=True, replace_existing=True, port=self.port)
        assert r["ok"] and r["deleted"] == n
        # 清理后不应残留旧构件（新写的那些是本次的）
        after = M.rwy_build(dry_run=True, port=self.port)["existing_tagged"]
        assert after > 0 and after != n + n

    def test_未确认时绝不删除(self, monkeypatch, mock_server, arch_dxf, tmp_path):
        from revit_wy import mcp_server as M
        self._prep(monkeypatch, mock_server, arch_dxf, tmp_path)
        assert M.rwy_build(dry_run=False, confirm=True, port=self.port)["ok"]
        before = M.rwy_build(dry_run=True, port=self.port)["existing_tagged"]
        bad = M.rwy_build(dry_run=False, confirm=False, replace_existing=True, port=self.port)
        assert bad["ok"] is False
        assert M.rwy_build(dry_run=True, port=self.port)["existing_tagged"] == before

    def test_连不上Revit时如实说不知道(self, monkeypatch, arch_dxf, tmp_path):
        """关键诚实性：连不上就不能报 existing_tagged=0。"""
        from revit_wy import mcp_server as M
        M._S["ir"] = None
        M._S["mapper"] = None
        monkeypatch.setattr(M, "WORK_DIR", tmp_path / "work")
        assert M.rwy_dwg_to_model(str(arch_dxf), "arch")["ok"]
        d = M.rwy_build(dry_run=True, port=_free_port())     # 没人监听
        assert d["ok"] and d["revit_reachable"] is False
        assert any("连不上" in c for c in d["check"])
