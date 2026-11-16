"""体检链路的端到端验证：MCP 工具 → 假 Revit → 规则 → 报告。

单测规则只能证明"判定对"，这里证明"接线对"：
工具名、参数表、返回结构、落盘文件都要能跑通。
"""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import revit_wy.mcp_server as M


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def mock_revit_server():
    import mock_revit as mr
    mr.STATE.__init__()
    port = _free_port()
    srv = mr.Server(("127.0.0.1", port), mr.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


class TestAuditTools:
    def test_巡检能出问题清单(self, mock_revit_server):
        r = M.rwy_audit_model(port=mock_revit_server)
        assert r["ok"] is True
        assert r["counts"]["elements"] > 0
        assert r["score"] < 100                 # 仿真模型故意不干净
        assert "PARAM-001" in r["by_rule"]
        assert r["fixable"]["auto_fixable_count"] > 0

    def test_巡检只读_不改模型(self, mock_revit_server):
        a = M.rwy_audit_model(port=mock_revit_server)
        b = M.rwy_audit_model(port=mock_revit_server)
        assert a["score"] == b["score"]
        assert a["fixable"] == b["fixable"]

    def test_返回大清单会截断(self, mock_revit_server):
        r = M.rwy_audit_model(port=mock_revit_server)
        assert len(r["issues"]) <= 50
        assert isinstance(r["issues_truncated"], bool)

    def test_能落盘html和md(self, mock_revit_server, tmp_path):
        h = tmp_path / "r" / "a.html"
        m = tmp_path / "r" / "a.md"
        r = M.rwy_audit_model(port=mock_revit_server, html_path=str(h),
                              markdown_path=str(m))
        assert r["ok"] and r["html"] == str(h)
        assert "模型体检报告" in h.read_text(encoding="utf-8")
        assert "按规则汇总" in m.read_text(encoding="utf-8")

    def test_fix默认干跑(self, mock_revit_server):
        r = M.rwy_fix_issues([1001, 1004], param="mark", value="待补",
                             port=mock_revit_server)
        assert r["dry_run"] is True and r["would_change"] == 2

    def test_fix给空ids必须拒绝(self, mock_revit_server):
        r = M.rwy_fix_issues([], port=mock_revit_server)
        assert r["ok"] is False            # 不能"全都改"，必须显式给 id

    def test_export导出路径(self, mock_revit_server):
        r = M.rwy_export_audit(port=mock_revit_server)
        assert r["ok"] and r["json"].endswith(".json")


class TestCliWiring:
    def test_audit子命令已注册(self):
        from revit_wy.cli import build_parser
        args = build_parser().parse_args(["audit", "model", "--demo"])
        assert args.func.__name__ == "cmd_audit"
        assert args.action == "model" and args.demo is True

    def test_fix默认不写(self):
        from revit_wy.cli import build_parser
        args = build_parser().parse_args(["audit", "fix", "--ids", "1,2"])
        assert args.apply is False
