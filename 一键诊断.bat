@echo off
REM ===========================================================================
REM  revit-wy 一键诊断
REM
REM  安装/连接出问题时双击这个。它会把环境信息收集成一个 txt 文件，
REM  把那个文件发给支持人员即可定位问题。
REM ===========================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "REPORT=诊断报告_%COMPUTERNAME%_%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.txt"
set "REPORT=%REPORT: =_%"
set "REPORT=%REPORT:/=-%"

echo.
echo ===========================================================
echo   revit-wy 诊断
echo ===========================================================
echo.

> "%REPORT%" (
  echo ===== revit-wy 诊断报告 =====
  echo 生成时间: %DATE% %TIME%
  echo 计算机名: %COMPUTERNAME%
  echo 用户名:   %USERNAME%
  echo.
  echo ----- 1. Python -----
)
if exist ".venv\Scripts\python.exe" (
    echo   [OK] 虚拟环境存在
    echo [OK] .venv 存在 >> "%REPORT%"
    ".venv\Scripts\python.exe" --version >> "%REPORT%" 2>&1
    ".venv\Scripts\python.exe" -c "import ezdxf,yaml,mcp;print('ezdxf',ezdxf.__version__);import mcp;print('mcp ok')" >> "%REPORT%" 2>&1
) else (
    echo   [X] 没有虚拟环境，请先双击「一键启动.bat」
    echo [X] 无 .venv >> "%REPORT%"
)

echo.
echo ----- 2. Revit -----
>> "%REPORT%" echo.
>> "%REPORT%" echo ----- 2. Revit -----
set "REVIT=0"
if exist "C:\Program Files\Autodesk\Revit 2022\Revit.exe" (
    echo   [OK] Revit 2022 已安装
    >> "%REPORT%" echo [OK] Revit 2022 已安装
    set "REVIT=1"
) else (
    echo   [X] 未找到 Revit 2022（C:\Program Files\Autodesk\Revit 2022）
    >> "%REPORT%" echo [X] 未找到 Revit 2022
)

set "ADDIN=%APPDATA%\Autodesk\Revit\Addins\2022\RevitWy.Plugin.addin"
if exist "%ADDIN%" (
    echo   [OK] 插件清单已部署
    >> "%REPORT%" echo [OK] 插件清单: %ADDIN%
    if exist "%APPDATA%\Autodesk\Revit\Addins\2022\revit-wy\RevitWy.Plugin.dll" (
        echo   [OK] 插件 DLL 存在
        >> "%REPORT%" echo [OK] DLL 存在
    ) else (
        echo   [X] 缺 DLL
        >> "%REPORT%" echo [X] 缺 DLL
    )
) else (
    echo   [X] 未部署插件
    >> "%REPORT%" echo [X] 未部署插件
)

echo.
echo ----- 3. 端口连通性 -----
>> "%REPORT%" echo.
>> "%REPORT%" echo ----- 3. 端口 -----
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import socket;s=socket.socket();s.settimeout(2);r=s.connect_ex(('127.0.0.1',48880));print('[Revit插件 48880]','连通' if r==0 else '未连通(Revit未开或插件未加载)');s.close();s=socket.socket();s.settimeout(2);r=s.connect_ex(('127.0.0.1',8770));print('[工作台 8770]','运行中' if r==0 else '未运行');s.close()" >> "%REPORT%" 2>&1
)
type nul >nul
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import socket;s=socket.socket();s.settimeout(2);r=s.connect_ex(('127.0.0.1',48880));print('  [Revit插件 48880]','连通' if r==0 else '未连通');s.close();s=socket.socket();s.settimeout(2);r=s.connect_ex(('127.0.0.1',8770));print('  [工作台 8770]','运行中' if r==0 else '未运行');s.close()"
)

echo.
echo ----- 4. MCP 配置 -----
>> "%REPORT%" echo.
>> "%REPORT%" echo ----- 4. MCP 配置 -----
if exist "%USERPROFILE%\.cursor\mcp.json" (
    echo   [OK] Cursor 配置存在
    >> "%REPORT%" echo [OK] Cursor mcp.json
    type "%USERPROFILE%\.cursor\mcp.json" >> "%REPORT%" 2>&1
) else (
    echo   [ ] 无 Cursor 配置
    >> "%REPORT%" echo [ ] 无 Cursor 配置
)
if exist "%USERPROFILE%\.workbuddy\mcp.json" (
    echo   [OK] WorkBuddy 配置存在（记得点 Trust）
    >> "%REPORT%" echo [OK] WorkBuddy mcp.json
) else (
    echo   [ ] 无 WorkBuddy 配置
    >> "%REPORT%" echo [ ] 无 WorkBuddy 配置
)

echo.
echo ----- 5. 插件日志（最后 40 行）-----
>> "%REPORT%" echo.
>> "%REPORT%" echo ----- 5. plugin.log -----
set "LOG=%APPDATA%\revit-wy\plugin.log"
if exist "%LOG%" (
    powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 40" >> "%REPORT%" 2>&1
    echo   [OK] 已收集日志
) else (
    echo   [ ] 无插件日志（插件从未启动过）
    >> "%REPORT%" echo [ ] 无 plugin.log
)

echo.
echo ===========================================================
echo   诊断完成
echo   报告文件：%REPORT%
echo.
echo   请把这个文件发给支持人员。
echo ===========================================================
echo.
pause
