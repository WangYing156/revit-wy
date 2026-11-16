@echo off
REM ===========================================================================
REM  revit-wy 一键启动（Windows）
REM
REM  双击即可运行。首次运行会自动：
REM    1. 检查 Python
REM    2. 建虚拟环境并装依赖
REM    3. 编译并部署 Revit 插件（若检测到 Revit 2022 与 .NET SDK）
REM    4. 安装 MCP 配置（Cursor / WorkBuddy）
REM    5. 用样例图自检五阶段流水线
REM    6. 启动数模对照工作台并打开浏览器
REM
REM  之后再次双击 = 直接启动工作台。
REM  想重置：删除仓库下的 .venv 目录后重新双击。
REM ===========================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "GREEN=[92m"
set "YELLOW=[93m"
set "RED=[91m"
set "CYAN=[96m"
set "DIM=[90m"
set "RESET=[0m"

echo.
echo %CYAN%===========================================================%RESET%
echo %CYAN%  revit-wy  ｜  AutoCAD 到 Revit 2022 一键翻模%RESET%
echo %CYAN%===========================================================%RESET%
echo.

REM ---------------------------------------------------------------- 1. Python
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYCMD=py -3"
    goto :py_found
)
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYCMD=python"
    goto :py_found
)

echo %RED%[X] 未找到 Python%RESET%
echo.
echo     请先安装 Python 3.9 或更高版本（安装时务必勾选 Add Python to PATH）
echo     下载：https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:py_found
for /f "tokens=2" %%v in ('%PYCMD% --version 2^>^&1') do set "PYVER=%%v"
echo %GREEN%[1/6]%RESET% Python !PYVER!   ^(%PYCMD%^)

REM ---------------------------------------------------------------- 2. venv
set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
    echo %YELLOW%      首次运行，创建虚拟环境（约 1 分钟）…%RESET%
    %PYCMD% -m venv "%VENV%"
    if not exist "%VPY%" (
        echo %RED%[X] 虚拟环境创建失败%RESET%
        echo     尝试手动执行：%PYCMD% -m venv .venv
        pause
        exit /b 1
    )
)

REM 依赖检查：只查关键包，避免每次 pip 都要联网
"%VPY%" -c "import ezdxf, yaml, mcp" >nul 2>&1
if not %errorlevel%==0 (
    echo %YELLOW%      安装依赖（首次或版本变更，约 1-3 分钟）…%RESET%
    "%VPY%" -m pip install --upgrade pip -q
    "%VPY%" -m pip install -q -e .
    if not %errorlevel%==0 (
        echo %RED%[X] 依赖安装失败%RESET%
        echo     常见原因：网络不通 / 公司代理限制
        echo     可试：pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
        echo     然后重新双击本脚本
        pause
        exit /b 1
    )
)
echo %GREEN%[2/6]%RESET% 依赖就绪

REM ---------------------------------------------------------------- 3. 插件
set "ADDIN_DIR=%APPDATA%\Autodesk\Revit\Addins\2022"
set "ADDIN_FILE=%ADDIN_DIR%\RevitWy.Plugin.addin"

set "REVIT_OK=0"
if exist "C:\Program Files\Autodesk\Revit 2022\Revit.exe" set "REVIT_OK=1"

if "%REVIT_OK%"=="0" (
    echo %YELLOW%[3/6]%RESET% 未检测到 Revit 2022，跳过插件部署
    echo %DIM%      ^(仅影响「写入 Revit」，图纸清洗/对照/预览/导出均可正常使用^)%RESET%
    goto :skip_addin
)

if exist "%ADDIN_FILE%" (
    echo %GREEN%[3/6]%RESET% Revit 插件已部署
    goto :skip_addin
)

where dotnet >nul 2>&1
if not %errorlevel%==0 (
    echo %YELLOW%[3/6]%RESET% 检测到 Revit，但缺少 .NET SDK，无法编译插件
    echo %DIM%     安装 .NET SDK 6.0+ 后重新双击即可自动编译部署%RESET%
    echo %DIM%     下载：https://dotnet.microsoft.com/download%RESET%
    goto :skip_addin
)

echo %YELLOW%      编译 Revit 插件（首次约 1-2 分钟）…%RESET%
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-addin.ps1" -Deploy
if %errorlevel%==0 (
    echo %GREEN%[3/6]%RESET% Revit 插件已编译并部署
) else (
    echo %YELLOW%[3/6]%RESET% 插件编译失败，其余功能不受影响
    echo %DIM%     详情：在 PowerShell 里执行 scripts\build-addin.ps1 看完整报错%RESET%
)

:skip_addin

REM ---------------------------------------------------------------- 4. MCP 配置
echo %GREEN%[4/6]%RESET% 检查 MCP 客户端配置…

set "SRC_DIR=%~dp0src"
if "%SRC_DIR:~-1%"=="\" set "SRC_DIR=%SRC_DIR:~0,-1%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$py='%~dp0.venv\Scripts\python.exe'.Replace('\\','\\\\'); $src='%SRC_DIR%'.Replace('\\','\\\\');" ^
  "$cfg='{\"mcpServers\":{\"revit-wy\":{\"command\":\"'+$py+'\",\"args\":[\"-m\",\"revit_wy.mcp_server\",\"--profile\",\"arch\"],\"env\":{\"PYTHONPATH\":\"'+$src+'\"}}}}';" ^
  "$c=Join-Path $env:USERPROFILE '.cursor\mcp.json';" ^
  "if(-not (Test-Path $c)){ New-Item -ItemType Directory -Force (Split-Path $c) | Out-Null; $cfg | Set-Content $c -Encoding UTF8; Write-Host '     已写入 Cursor 配置：' $c }" ^
  "else { $cfg | Set-Content ($c + '.revit-wy.json') -Encoding UTF8; Write-Host '     Cursor 已有配置，新版写入 mcp.json.revit-wy.json，请手工合并' }" ^
  "$wb=Join-Path $env:USERPROFILE '.workbuddy\mcp.json';" ^
  "if(-not (Test-Path $wb)){ New-Item -ItemType Directory -Force (Split-Path $wb) | Out-Null; $cfg | Set-Content $wb -Encoding UTF8; Write-Host '     已写入 WorkBuddy 配置（需在客户端点 Trust）：' $wb }" ^
  "else { $cfg | Set-Content ($wb + '.revit-wy.json') -Encoding UTF8; Write-Host '     WorkBuddy 已有配置，新版写入 mcp.json.revit-wy.json' }"

REM ---------------------------------------------------------------- 5. 启动
REM 启动前先做一次五阶段自检：确保 pipeline 真能跑（不是只看依赖装没装）
echo %YELLOW%      自检流水线（用内置样例图跑一遍五阶段）…%RESET%
"%VPY%" -m revit_wy.cli dwg samples\arch_plan.dxf --out "%TEMP%\revit-wy-selftest" >nul 2>&1
if %errorlevel%==0 (
    echo %GREEN%[5/6]%RESET% 流水线自检通过
) else (
    echo %YELLOW%[5/6]%RESET% 自检未通过，但工作台仍可启动
    echo %DIM%     排错：双击「一键诊断.bat」生成报告%RESET%
)

echo %GREEN%[6/6]%RESET% 启动数模对照工作台…

REM 选一张样例图预加载，方便立刻看到效果
set "SAMPLE=samples\arch_plan.dxf"
if not exist "%SAMPLE%" set "SAMPLE="

echo.
echo %CYAN%-----------------------------------------------------------%RESET%
echo   工作台地址：%CYAN%http://127.0.0.1:8770/%RESET%
echo   %DIM%关闭此窗口即停止服务。图纸路径可在页面里改。%RESET%
echo %CYAN%-----------------------------------------------------------%RESET%
echo.

REM 延迟 3 秒再开浏览器，等 Flask/http 服务起来
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8770/"

if defined SAMPLE (
    "%VPY%" tools\viewer\server.py "%SAMPLE%" --host 127.0.0.1 --port 8770
) else (
    "%VPY%" tools\viewer\server.py --host 127.0.0.1 --port 8770
)

echo.
echo 服务已停止。
pause
