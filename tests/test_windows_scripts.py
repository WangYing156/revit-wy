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


class TestLauncherDelegation:
    """编排逻辑必须留在 Python 里。

    前四轮"跑不起来"全部是 cmd 语法/编码问题，而 cmd 没有异常、没有栈、
    没有日志——失败信息在屏幕上一闪就没。所以 .bat 只允许做两件事：
    找到 Python、把控制权交给 setup_launcher。
    """

    def test_启动脚本只做委派(self):
        text = _bat("一键启动.bat").decode("ascii")
        assert "revit_wy.setup_launcher" in text
        # 不再自己装依赖/编译/起服务
        assert "pip install" not in text, "pip 不能留在 cmd 里"
        assert "build-addin.ps1" not in text, "编译不能留在 cmd 里"
        assert "server.py" not in text, "起服务不能留在 cmd 里"

    def test_启动脚本仍能自举venv(self):
        """光有委派不够：全新机器上 .venv 还不存在，得先建出来。"""
        text = _bat("一键启动.bat").decode("ascii")
        assert "-m venv" in text
        assert "goto :bootstrap" in text

    def test_启动脚本不再有括号块(self):
        """"set 在 if(...) 里" 会吞掉右括号，这是踩过的坑。"""
        text = _bat("一键启动.bat").decode("ascii")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("REM") or not stripped:
                continue
            assert not stripped.endswith(")"), f"疑似括号块：{line!r}"
            assert "(" not in stripped or stripped.startswith("echo"), line

    def test_诊断脚本会打印启动日志(self):
        text = _bat("一键诊断.bat").decode("ascii")
        assert "setup-*.log" in text
        assert "type " in text


class TestLauncher:
    """setup_launcher 是真正的编排器，得能跑、能收敛、能落盘。"""

    def test_能跑完并给出结论(self, tmp_path):
        from revit_wy import setup_launcher
        log = tmp_path / "setup.log"
        rc = setup_launcher.run(ROOT, no_viewer=True, log_path=str(log))
        assert rc in (0, 2)
        assert log.exists()

    def test_日志带BOM且记录关键信息(self, tmp_path):
        from revit_wy import setup_launcher
        log = tmp_path / "setup.log"
        setup_launcher.run(ROOT, no_viewer=True, log_path=str(log))
        raw = log.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "记事本打开不能乱码"
        text = raw.decode("utf-8-sig")
        assert "platform" in text and "python" in text
        assert "结论" in text

    def test_日志关闭后写不炸(self):
        from revit_wy.setup_launcher import Log
        import tempfile
        log = Log(Path(tempfile.mkdtemp()) / "x.log")
        log.close()
        log.write("still safe")     # 退出路径上重复写不该抛
        log.close()                 # close 必须幂等

    def test_失败会分类给出下一步(self, tmp_path, monkeypatch):
        """不能只报"失败了"，要给出可执行的动作。"""
        from revit_wy import setup_launcher as L
        out = L.Outcome()
        log = L.Log(tmp_path / "y.log")
        # 把"所有可能的 Python"都换成必然失败的命令，确保走失败分支
        monkeypatch.setattr(L, "_which_python", lambda: [["rwy-no-such-python"]])
        vpy = L.step_venv(tmp_path / "nope", log, out)
        log.close()
        assert vpy is None
        assert out.ok is False
        assert out.blockers and out.next_steps

    def test_结果对象区分阻断与降级(self):
        from revit_wy.setup_launcher import Outcome
        o = Outcome()
        o.degrade("没装 Revit")
        assert o.ok is True and o.degraded
        o.block("依赖装不上")
        assert o.ok is False and o.blockers


class TestInstallerPaths:
    """这些坑曾经都在 .bat 里，现在搬进 setup_launcher.py —— 断言也跟着搬。"""

    def _launcher(self) -> str:
        return (ROOT / "src/revit_wy/setup_launcher.py").read_text(encoding="utf-8")

    def test_pip用绝对路径且不加点后缀(self):
        """曾经的 bug：-e "%ROOT%." 会拼成 F:\\repo\\.  这是"根本跑不起来"的根因。"""
        text = self._launcher()
        assert '"install", "-e", str(root)' in text
        assert 'str(root) + "."' not in text

    def test_pip失败有三级兜底(self):
        text = self._launcher()
        assert "tuna.tsinghua.edu.cn" in text
        assert "--only-binary=:all:" in text
        assert "直连 PyPI" in text

    def test_自检失败会明确停下并给出原因(self):
        text = self._launcher()
        assert "setup_check" in text
        assert 'rep.worst == "bad"' in text

    def test_浏览器在端口起来后再开(self):
        """start /b cmd /c "timeout ... & start url" 在某些环境里会挂住。"""
        text = self._launcher()
        assert "webbrowser.open" in text
        assert "threading.Timer" in text
        assert "cmd /c" not in text

    def test_端口被占时不再起服务(self):
        text = self._launcher()
        assert "已被占用" in text
        assert "def step_ports" in text


class TestPowerShellEncoding:
    @pytest.mark.parametrize("name", ["build-addin.ps1", "install.ps1"])
    def test_带UTF8_BOM(self, name):
        """无 BOM 时 Windows PowerShell 5.1 按 GBK 解码，中文注释会把脚本解析坏。"""
        raw = (ROOT / "scripts" / name).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{name} 缺 UTF-8 BOM"


class TestNet48Build:
    """MSB3644 的根因是缺 .NET Framework 4.8 引用程序集。

    这条链上任何一环退回去，用户都会重新掉进同一个坑：
      工程没有 WorkloadResolver 开关 → SDK 回去解析工作负载 → MSB3644
      PowerShell 不做前置探测 → 用户只看到英文错误码
    """

    def _csproj(self) -> str:
        return (ROOT / "addin/RevitWy.Plugin/RevitWy.Plugin.csproj").read_text(encoding="utf-8")

    def test_关闭工作负载解析(self):
        assert "<MSBuildEnableWorkloadResolver>false</MSBuildEnableWorkloadResolver>" in self._csproj()

    def test_编译前检查v48引用程序集(self):
        text = self._csproj()
        assert "CheckNet48ReferenceAssemblies" in text
        assert "BeforeTargets=\"ResolveAssemblyReferences\"" in text
        assert "mscorlib.dll" in text

    def test_net48用12版Newtonsoft避免运行时崩(self):
        """引用 13.x 会报 Could not load Newtonsoft.Json, Version=13.0.0.0。"""
        text = self._csproj()
        assert 'Version="12.0.3"' in text
        assert "TargetFramework)' == 'net48'" in text

    def test_有绑定重定向文件(self):
        cfg = ROOT / "addin/RevitWy.Plugin/app.net48.config"
        assert cfg.exists()
        text = cfg.read_text(encoding="utf-8")
        assert "Newtonsoft.Json" in text
        assert 'newVersion="12.0.0.0"' in text

    def test_绑定重定向会被部署(self):
        assert "RevitWy.Plugin.dll.config" in (ROOT / "scripts/build-addin.ps1").read_text(
            encoding="utf-8-sig")

    def test_ps1会提前探测引用程序集(self):
        text = (ROOT / "scripts/build-addin.ps1").read_text(encoding="utf-8-sig")
        assert "MSB3644" in text
        assert "Reference Assemblies" in text
        assert "Developer Pack" in text

    def test_先试Nuget再回退本地API(self):
        text = (ROOT / "src/revit_wy/setup_launcher.py").read_text(encoding="utf-8")
        nuget = text.index('"-UseNuget"')
        fallback = text.index("改用本地 RevitAPI.dll 重试")
        assert nuget < fallback, "应当先试不需要 Framework 开发包的 NuGet 路径"

    def test_所有xml注释里没有连续双减号(self):
        """XML 注释里出现 -- 会让 MSBuild/expat 直接判 not well-formed。"""
        import re
        for f in ["addin/RevitWy.Plugin/RevitWy.Plugin.csproj",
                  "addin/RevitWy.Plugin/app.net48.config",
                  "addin/RevitWy.Plugin/RevitWy.Plugin.addin"]:
            text = (ROOT / f).read_text(encoding="utf-8")
            for m in re.finditer(r"<!--(.*?)-->", text, re.S):
                body = m.group(1)
                assert "--" not in body, f"{f} 注释里有连续双减号"


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
        for must in ("python", "venv", "deps", "config", "viewer", "bat", "net48"):
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
