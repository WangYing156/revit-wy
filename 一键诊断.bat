@echo off
REM ===========================================================================
REM  revit-wy diagnostics  (one-click)
REM
REM  All check logic lives in Python (src/revit_wy/diagnose.py). This file only
REM  locates Python and calls it.
REM
REM  Why not pure batch: ">> file echo ..." mixed with if(...) blocks gets
REM  mis-parsed by cmd.exe. The previous version printed
REM      'EPORT" echo [X] ...' is not recognized
REM  which hid the real problems. Keep this file ASCII-only and dumb.
REM ===========================================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo ===========================================================
echo   revit-wy diagnostics
echo ===========================================================
echo.

set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" goto :have_py

echo   [X] No virtualenv found at .venv
echo.
echo       Run the setup .bat in this folder first (one-click start).
echo       It creates .venv and installs everything.
echo.
pause
exit /b 1

:have_py
"%VPY%" -m revit_wy.diagnose --root "%~dp0" %*
if errorlevel 2 goto :self_error

echo.
echo   Report saved next to this file.
echo   Send that .txt to whoever supports you.
echo.
pause
exit /b 0

:self_error
echo.
echo   [X] The diagnostic script itself crashed.
echo       Please screenshot the text above and send it over.
echo.
pause
exit /b 2
