@echo off
REM ===========================================================================
REM  revit-wy  one-click setup (Windows)
REM
REM  This file does exactly ONE thing: find a Python and hand over to
REM  revit_wy.setup_launcher. All real work happens there.
REM
REM  Why it is this short now
REM  ------------------------
REM  Previous versions orchestrated venv/pip/build/viewer in batch. Every
REM  failure so far came from cmd.exe itself, not from Python:
REM      UTF-8 Chinese read as GBK  -> garbled commands
REM      LF line endings            -> goto/label silently broken
REM      -e "%ROOT%."               -> pip got an invalid path
REM      set inside if(...) blocks  -> variables swallowing ")"
REM      "one line of error"        -> no usable diagnostic
REM  cmd has no exceptions, no stack traces and no log. Keep it dumb.
REM
REM  Style rules for this file:
REM    * ASCII only, CRLF line endings, no BOM
REM    * no parentheses blocks, no nested quotes, goto/labels only
REM ===========================================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo.
echo ===========================================================
echo   revit-wy  ^|  AutoCAD to Revit 2022+ one-click setup
echo ===========================================================
echo.

REM ---------------------------------------------------------------- 1. reuse .venv
set "VPY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VPY%" goto :run_venv

REM ---------------------------------------------------------------- 2. any python
set "PYCMD="
where py >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :bootstrap
where python >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto :bootstrap
goto :no_python

:bootstrap
echo   Python found. Creating the virtual environment (about 1 minute)...
echo.
%PYCMD% -m venv "%ROOT%\.venv"
if not exist "%VPY%" goto :venv_failed
echo.
echo   Virtual environment ready. Continuing...
goto :run_venv

:run_venv
set "PYTHONPATH=%ROOT%\src"
"%VPY%" -m revit_wy.setup_launcher --root "%ROOT%"
if errorlevel 2 goto :had_problems

echo.
echo   Done. Press any key to close.
pause >nul
exit /b 0

REM ---------------------------------------------------------------- errors
:no_python
echo   [X] No Python found on this machine.
echo.
echo       Install Python 3.9 or newer from python.org, and tick
echo       "Add python.exe to PATH" during setup:
echo         https://www.python.org/downloads/windows/
echo.
echo       Then double-click this file again.
echo.
pause
exit /b 1

:venv_failed
echo   [X] Could not create .venv with "%PYCMD%".
echo.
echo       Run this in this folder to see the real error:
echo         %PYCMD% -m venv .venv
echo.
pause
exit /b 1

:had_problems
echo.
echo   Setup finished but something needs your attention - see the list above.
echo   The full log is in the reports folder; send that file over.
echo.
pause
exit /b 2
