@echo off
REM ===========================================================================
REM  revit-wy  one-click setup (Windows)
REM
REM  Double-click this file. It will:
REM    1. find Python
REM    2. create .venv and install deps
REM    3. compile + deploy the Revit 2022 add-in (skipped if no Revit)
REM    4. write MCP config for Cursor / WorkBuddy (merge, never overwrite)
REM    5. self-test the 5-stage pipeline
REM    6. start the workspace and open the browser
REM
REM  Style rules for this file (learned the hard way):
REM    * ASCII only  - cmd.exe mangles UTF-8 inside blocks
REM    * goto/labels instead of big if(...) blocks with redirection
REM    * all real logic lives in Python (revit_wy.diagnose / install_mcp)
REM  Chinese explanations are printed by the Python side.
REM ===========================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
REM Strip the trailing backslash from %~dp0. On Windows a trailing \ before a
REM closing quote ESCAPES the quote (MSVC argv rules), so
REM   --root "D:\repo\"  --python "..."
REM becomes a single argument and every path silently breaks.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo.
echo ===========================================================
echo   revit-wy  ^|  AutoCAD to Revit 2022 one-click
echo ===========================================================
echo.

REM ------------------------------------------------------------- 1. Python
set "PYCMD="
where py >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :py_found
where python >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto :py_found

echo [X] Python not found.
echo.
echo     Install Python 3.9+ and check "Add Python to PATH" during setup.
echo     Download: https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:py_found
for /f "tokens=2" %%v in ('%PYCMD% --version 2^>^&1') do set "PYVER=%%v"
echo [1/6] Python !PYVER!

REM ------------------------------------------------------------- 2. venv
set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" goto :venv_ready

echo       Creating virtualenv (about 1 minute)...
%PYCMD% -m venv "%ROOT%\.venv"
if not exist "%VPY%" goto :venv_fail

:venv_ready
"%VPY%" -c "import ezdxf, yaml, mcp" >nul 2>&1
if not errorlevel 1 goto :deps_ok

echo       Installing dependencies (1-3 minutes)...
"%VPY%" -m pip install --upgrade pip -q
"%VPY%" -m pip install -q -e "%ROOT%."
if errorlevel 1 (
    echo       Retrying with Tsinghua mirror...
    "%VPY%" -m pip install -q -e "%ROOT%." -i https://pypi.tuna.tsinghua.edu.cn/simple
)
"%VPY%" -c "import ezdxf, yaml, mcp" >nul 2>&1
if errorlevel 1 goto :deps_fail
goto :deps_ok

:venv_fail
echo [X] Failed to create .venv
echo     Try manually:  %PYCMD% -m venv .venv
echo.
pause
exit /b 1

:deps_fail
echo [X] Failed to install dependencies
echo     Usual cause: network / corporate proxy.
echo     Try:  .venv\Scripts\pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:deps_ok
echo [2/6] Dependencies ready

REM ------------------------------------------------------------- 3. add-in
REM Supported: Revit 2022 and newer (2022-2024 -> net48, 2025+ -> .NET 8).
REM NOTE: inside a parenthesised block, "set X=1" next to ")" is ambiguous.
REM Set with quotes and read the value back after the block, so nothing
REM depends on in-block variable expansion.
set "REVIT_FOUND=0"
set "ADDIN_OK=0"
for %%v in (2022 2023 2024 2025 2026 2027) do call :probe "%%v"
goto :probe_done

:probe
if exist "C:\Program Files\Autodesk\Revit %~1\Revit.exe" set "REVIT_FOUND=1"
if exist "%APPDATA%\Autodesk\Revit\Addins\%~1\RevitWy.Plugin.addin" set "ADDIN_OK=1"
goto :eof

:probe_done
if "%REVIT_FOUND%"=="0" goto :no_revit
if "%ADDIN_OK%"=="1" goto :addin_done

where dotnet >nul 2>&1
if errorlevel 1 goto :no_dotnet

echo       Compiling Revit add-in (1-2 minutes per installed version)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-addin.ps1" -Deploy
if errorlevel 1 goto :addin_fail
echo [3/6] Add-in compiled and deployed
goto :addin_done

:no_revit
echo [3/6] No Revit 2022+ found - skipping add-in
echo       Everything except "write into Revit" still works.
echo       Try it now:  .venv\Scripts\python -m revit_wy.cli demo
goto :addin_done

:no_dotnet
echo [3/6] Revit found but .NET SDK missing - cannot compile
echo       Install .NET SDK 6.0+ then re-run: https://dotnet.microsoft.com/download
goto :addin_done

:addin_fail
echo [3/6] Add-in build failed - other features unaffected
echo       Run scripts\build-addin.ps1 in PowerShell to see the full error
goto :addin_done

:addin_done

REM ------------------------------------------------------------- 4. MCP config
echo [4/6] Writing MCP client config...
"%VPY%" -m revit_wy.install_mcp --root "%ROOT%" --python "%VPY%"
if errorlevel 1 echo       (partial failure - see messages above)

REM ------------------------------------------------------------- 5. self-test
echo [5/6] Self-test (5-stage pipeline on a sample drawing)...
"%VPY%" -m revit_wy.cli dwg "%ROOT%\samples\arch_plan.dxf" --out "%~TEMP%\revit-wy-selftest" >nul 2>&1
if errorlevel 1 goto :selftest_fail
echo       Pipeline OK
goto :start_viewer

:selftest_fail
echo       [!] Self-test failed - workspace still works
echo       Double-click the diagnostic .bat in this folder for a report.

REM ------------------------------------------------------------- 6. viewer
:start_viewer
echo [6/6] Starting workspace...
echo.
echo -----------------------------------------------------------
echo   Workspace:  http://127.0.0.1:8770/
echo   Close this window to stop the server.
echo -----------------------------------------------------------
echo.

start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8770/"

if exist "%~dp0samples\arch_plan.dxf" (
    "%VPY%" "%ROOT%\tools\viewer\server.py" "%ROOT%\samples\arch_plan.dxf" --host 127.0.0.1 --port 8770
) else (
    "%VPY%" "%ROOT%\tools\viewer\server.py" --host 127.0.0.1 --port 8770
)

echo.
echo Workspace stopped.
pause
