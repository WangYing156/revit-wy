@echo off
REM ===========================================================================
REM  revit-wy  one-click setup (Windows)
REM
REM  Double-click this file. It will:
REM    1. find Python
REM    2. create .venv and install deps
REM    3. compile + deploy the add-in for every installed Revit 2022+ (skip if none)
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
echo   revit-wy  ^|  AutoCAD to Revit 2022+ one-click
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
"%VPY%" -m pip install -q -e "%ROOT%"
if not errorlevel 1 goto :pip_native_ok
echo       Retrying with Tsinghua mirror...
"%VPY%" -m pip install -q -e "%ROOT%" -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto :pip_native_ok
echo       Native wheels failed. Retrying with prebuilt-only (no compiler needed)...
"%VPY%" -m pip install -q -e "%ROOT%" --only-binary=:all: --prefer-binary
:pip_native_ok
"%VPY%" -c "import ezdxf, yaml, mcp" >nul 2>&1
if errorlevel 1 goto :deps_fail
REM Not fatal: converting .dwg -> .dxf needs COM/OLE. Missing it only means
REM .dxf input works and .dwg input asks for accoreconsole/ODA instead.
"%VPY%" -c "import win32com.client" >nul 2>&1
if not errorlevel 1 goto :deps_ok
echo       [i] pywin32 not installed - .dwg -> .dxf will need AutoCAD/ODA later.
echo           .dxf input works fine. Add it later with:
echo             .venv\Scripts\pip.exe install pywin32
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

REM Run the real self-check before doing anything else. Every failure here
REM prints a concrete next action, so a broken setup never ends in a pile of
REM red text with no hint. Exit code 2 = must fix, 0/1 = keep going.
echo.
echo [2/6] Self-check...
set "PYTHONPATH=%ROOT%\src"
"%VPY%" -m revit_wy.setup_check --root "%ROOT%"
if errorlevel 2 goto :selfcheck_bad

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
REM Try the NuGet route first: it does not need the .NET Framework 4.8
REM Developer Pack, which is the #1 reason the build fails on a fresh machine
REM (error MSB3644: reference assemblies for .NETFramework,Version=v4.8 not found).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-addin.ps1" -UseNuget -Deploy
if not errorlevel 1 goto :addin_compiled
echo.
echo       NuGet route failed. Retrying with the local Revit API DLLs...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-addin.ps1" -Deploy
if not errorlevel 1 goto :addin_compiled
goto :addin_fail

:addin_compiled
echo [3/6] Add-in compiled and deployed
goto :addin_done

:no_revit
echo [3/6] No Revit 2022+ found - skipping add-in
echo       Everything except "write into Revit" still works.
echo       Try it now:  ".venv\Scripts\python.exe" -m revit_wy.cli demo
goto :addin_done

:no_dotnet
echo [3/6] Revit found but .NET SDK missing - cannot compile
echo       Install .NET SDK 6.0+ then re-run: https://dotnet.microsoft.com/download
goto :addin_done

:addin_fail
echo [3/6] Add-in build failed - other features unaffected
echo.
echo   The usual cause is a missing .NET Framework 4.8 reference assemblies
echo   pack (error MSB3644). Pick ONE of these:
echo     1) Install .NET Framework 4.8 Developer Pack (small, ~70 MB)
echo        https://dotnet.microsoft.com/download/dotnet-framework/net48
echo     2) Install Visual Studio Build Tools with ".NET Framework 4.8
echo        targeting pack" checked
echo     3) Skip Revit 2022-2024 and build for 2025+ only:
echo        powershell -File scripts\build-addin.ps1 -RevitVersions 2025 -UseNuget -Deploy
echo   Everything except "write into Revit" keeps working either way.
goto :addin_done

:selfcheck_bad
echo.
echo [X] Self-check found problems that must be fixed first (see the list above).
echo     Nothing else was started - better than a half-working setup.
echo.
pause
exit /b 2

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

REM Port 8770 may already be taken by a previous run; tell the user instead of a wall of traceback.
"%VPY%" -c "import socket,sys;s=socket.socket();r=s.connect_ex(('127.0.0.1',8770));s.close();sys.exit(r)"
if not errorlevel 1 goto :port_busy

REM Open the browser from Python, 2s later, so it only happens after the port is up.
start "" /b "%VPY%" -c "import time,webbrowser;time.sleep(2);webbrowser.open('http://127.0.0.1:8770/')"

if exist "%ROOT%\samples\arch_plan.dxf" (
    "%VPY%" "%ROOT%\tools\viewer\server.py" "%ROOT%\samples\arch_plan.dxf" --host 127.0.0.1 --port 8770
) else (
    "%VPY%" "%ROOT%\tools\viewer\server.py" --host 127.0.0.1 --port 8770
)

echo.
echo Workspace stopped.
pause

:port_busy
echo.
echo [X] Port 8770 is already in use - maybe another copy of this window is running.
echo     Close it, or open http://127.0.0.1:8770/ directly to see if it is the workspace.
echo.
pause
exit /b 0
