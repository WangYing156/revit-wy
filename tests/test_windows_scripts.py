"""Windows 启动脚本与启动自检的静态/动态校验。

为什么要专门测 .bat
-------------------
这一层没有任何编译器帮你把关。前两次"根本运行不起来"都是脚本本身的
低级错误（路径拼接、编码、换行、cmd 语法），而每一次都表现为
"双击之后闪一下就没了"——用户拿不到任何线索。

所以这里把每条踩过的坑都变成一条断言。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATS = ["一键启动.bat", "一键诊断.bat", "一键体检.bat"]


def _bat(name: str) -> bytes:
    return (ROOT / name).read_bytes()


class TestBatEncoding:
    @pytest.mark.parametrize("name", BATS)
    def test_纯ASCII(self, name):
        """cmd.exe 在中文 Windows 上按 GBK 读 .bat，UTF-8 中文会变成乱码命令。"""
        raw = _bat(name)
        non_ascii = [b for b in raw if b > 127]
        assert not non_ascii, f"{name} 含 {len(non_ascii)} 个非 ASCII 字节"

    @pytest.mark.parametrize("name", BATS)
    def test_CRLF换行(self, name):
        """LF-only 的 .bat 在 cmd 里 goto/label 会失效。"""
        raw = _bat(name)
        assert b"\r\n" in raw
        assert raw.replace(b"\r\n", b"").count(b"\n") == 0

    @pytest.mark.parametrize("name", BATS)
    def test_无BOM(self, name):
        assert not _bat(name).startswith(b"\xef\xbb\xbf")


class TestBatLabels:
    @pytest.mark.parametrize("name", BATS)
    def test_所有goto都有对应label(self, name):
        text = _bat(name).decode("ascii").replace("\r\n", "\n")
        labels = set(re.findall(r"^:(\w+)", text, re.M))
        # call :sub 和 goto :label 都是引用，漏算 call 会把子过程误判成死代码
        gotos = set(re.findall(r"(?:goto|call) :(\w+)", text))
        assert not (gotos - labels - {"eof"}), f"{name} 跳到不存在的标签"
        # 无引用且不是子过程入口的标签 = 死代码，说明改坏了
        unused = labels - gotos - {"eof"}
        assert not unused, f"{name} 有孤立标签：{unused}"

    @pytest.mark.parametrize("name", BATS)
    def test_以echo_off开头(self, name):
        assert _bat(name).startswith(b"@echo off")

    @pytest.mark.parametrize("name", BATS)
    def test_结尾有pause或阻塞服务(self, name):
        """双击运行的脚本必须停下来，否则出错信息一闪就没。"""
        text = _bat(name).decode("ascii")
        assert "pause" in text


class TestInstallerPaths:
    def test_pip不再用拼错的项目路径(self):
        """曾经的 bug：-e "%ROOT%." 会拼成 F:\\repo\\.  这是"根本跑不起来"的根因。"""
        text = _bat("一键启动.bat").decode("ascii")
        assert '-e "%ROOT%."' not in text
        assert '-e "%ROOT%"' in text

    def test_pip失败有三级兜底(self):
        text = _bat("一键启动.bat").decode("ascii")
        assert "tuna.tsinghua.edu.cn" in text
        assert "--only-binary=:all:" in text

    def test_自检失败会明确停下并给出原因(self):
        text = _bat("一键启动.bat").decode("ascii")
        assert "revit_wy.setup_check" in text
        assert "goto :selfcheck_bad" in text

    def test_不再用_cmd_c加timeout开浏览器(self):
        """start /b cmd /c "timeout ... & start url" 在某些环境里会挂住。"""
        text = _bat("一键启动.bat").decode("ascii")
        assert "cmd /c \"timeout" not in text
        assert "webbrowser.open" in text

    def test_端口被占时给出提示(self):
        text = _bat("一键启动.bat").decode("ascii")
        assert "goto :port_busy" in text


class TestPowerShellEncoding:
    @pytest.mark.parametrize("name", ["build-addin.ps1", "install.ps1"])
    def test_带UTF8_BOM(self, name):
        """无 BOM 时 Windows PowerShell 5.1 按 GBK 解码，中文注释会把脚本解析坏。"""
        raw = (ROOT / "scripts" / name).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{name} 缺 UTF-8 BOM"


class TestSetupCheck:
    """自检模块自己也要被自检：它能跑、能给出结论、不误报。"""

    @pytest.fixture(scope="class")
    @classmethod
    def report(cls):
        from revit_wy import setup_check
        return setup_check.run(ROOT)

    def test_在本仓库能跑出结论(self, report):
        assert report.worst in ("ok", "warn", "bad")
        assert report.checks

    def test_必检项都在(self, report):
        keys = {c.key for c in report.checks}
        for must in ("python", "venv", "deps", "config", "viewer", "bat"):
            assert must in keys, must

    def test_每项失败都有可执行的下一步(self, report):
        """绝不出现"请联系管理员"这种废话。"""
        for c in report.checks:
            if c.level == "bad":
                assert c.fix and len(c.fix) > 8, f"{c.label} 没有给出修复方法"

    def test_本仓库路径下不误报Python版本(self, report):
        assert (sys.version_info.major, sys.version_info.minor) >= (3, 9)
        assert next(c for c in report.checks if c.key == "python").level == "ok"

    def test_工作台真能起服务(self, report):
        viewer = next(c for c in report.checks if c.key == "viewer")
        assert viewer.level == "ok", viewer.detail

    def test_按严重度给退出码(self, report):
        assert report.exit_code() == {"ok": 0, "warn": 0, "bad": 2}[report.worst]

    def test_给出JSON方便远程排查(self):
        from revit_wy import setup_check
        import json
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup_check.main(["--root", str(ROOT), "--json"])
        data = json.loads(buf.getvalue())
        assert data["root"] == str(ROOT.resolve())
        assert isinstance(data["checks"], list)


class TestDiagnosePreliminary:
    def test_诊断脚本先跑启动自检(self):
        text = _bat("一键诊断.bat").decode("ascii")
        assert "revit_wy.setup_check" in text
        # 顺序：启动自检在前，完整环境报告在后
        assert text.index("setup_check") < text.index("revit_wy.diagnose")
