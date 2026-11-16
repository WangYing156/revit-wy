@echo off
REM ===========================================================================
REM  revit-wy diagnostics  (one-click)
REM
REM  All check logic lives in Python (setup_check.py + diagnose.py).
REM  This file only locates Python and calls it.
REM
REM  Output of this run is what support needs. It prints, in order:
REM     1) launcher self-check  - why one-click setup does not work
REM     2) last setup log       - the full story of the last attempt
REM     3) environment report   - versions, ports, MCP config, add-in state
REM
REM  Style rules: ASCII only, CRLF, no parentheses blocks, goto/labels only.
REM  (See the one-click setup .bat header for why.)
REM ===========================================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo.
echo ===========================================================
echo   revit-wy diagnostics
echo ===========================================================
echo.

set "VPY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VPY%" goto :have_py

echo   [X] No virtualenv at .venv
echo.
echo       Run the one-click setup .bat once - it creates .venv.
echo.
pause
exit /b 1

:have_py
set "PYTHONPATH=%ROOT%\src"

echo   [1/3] Launcher self-check...
echo.
"%VPY%" -m revit_wy.setup_check --root "%ROOT%"

echo.
echo   [2/3] Most recent setup log
echo.
if not exist "%ROOT%\reports" goto :no_log
if not exist "%ROOT%\reports\setup-*.log" goto :no_log
for /f "delims=" %%f in ('dir /b /o-d "%ROOT%\reports\setup-*.log" 2^>nul') do call :showlog "%%f"
goto :after_log

:showlog
if defined SHOWN goto :eof
set "SHOWN=1"
echo   File: %ROOT%\reports\%~1
echo   ------------------------------------------------------------
type "%ROOT%\reports\%~1"
echo   ------------------------------------------------------------
goto :eof

:no_log
echo   No setup log yet - the one-click setup has not run on this machine.

:after_log
echo.
echo   [3/3] Environment report...
echo.
"%VPY%" -m revit_wy.diagnose --root "%ROOT%" %*
if errorlevel 2 goto :diag_crashed

echo.
echo   A full report .txt was written next to this folder.
echo.
echo   What to send if you need support:
echo     - that report .txt file, and
echo     - the reports\setup-*.log printed above
echo   Sending files beats screenshots: they contain the exact commands
echo   and their real output.
echo.
pause
exit /b 0

:diag_crashed
echo.
echo   [X] The diagnostic script itself crashed.
echo       Send the report .txt plus the text above.
echo.
pause
exit /b 2
