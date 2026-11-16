@echo off
REM ===========================================================================
REM  revit-wy  model audit (Windows)  --  read-only, zero risk
REM
REM  Double-click with a model open in Revit. It will:
REM    1. find the local .venv python (or fall back to python on PATH)
REM    2. scan the open model: unused/empty views, missing params,
REM       non-standard family names, unused types
REM    3. write reports: reports\model-audit-<time>.html and .md
REM
REM  It does NOT modify your model. Batch fixing parameters is a separate
REM  command on purpose (revit-wy audit fix ...), default is a dry run.
REM
REM  ASCII only, goto/labels instead of big if(...) blocks -- see README note.
REM ===========================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY="
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if defined PY goto :run
where python >nul 2>&1
if errorlevel 1 goto :nopython
set "PY=python"

:run
if not exist "%ROOT%\reports" mkdir "%ROOT%\reports"
set "STAMP=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%-%TIME:~0,2%%TIME:~3,2%"
set "STAMP=%STAMP: =0%"
set "HTML=%ROOT%\reports\model-audit-%STAMP%.html"
set "MD=%ROOT%\reports\model-audit-%STAMP%.md"

echo.
echo   Scanning the model open in Revit (read-only)...
echo.
set "PYTHONPATH=%ROOT%\src"
"%PY%" -m revit_wy.cli audit model --html "%HTML%" --md "%MD%"
if errorlevel 2 goto :norevit

echo.
echo   Reports written to:
echo     %HTML%
echo     %MD%
echo.
echo   Opening the HTML report...
start "" "%HTML%"
goto :done

:norevit
echo.
echo   Could not reach the Revit add-in on port 48880.
echo.
echo   1) Is Revit running with a model open?
echo   2) Is there a "revit-wy" tab in the Revit ribbon?
echo      - no tab  =^> the add-in is not loaded. Build and deploy it:
echo          powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-addin.ps1" -Deploy
echo          then restart Revit.
echo      - yes tab =^> open the dockable pane and check the port it shows.
echo   3) Two Revit instances cannot both hold 48880 - close the extra one.
echo.
echo   Full environment check:  "%PY%" -m revit_wy.setup_check --root "%ROOT%"
echo.
echo   Want to see what a report looks like without Revit?
echo     "%PY%" -m revit_wy.cli audit model --demo
goto :done

:nopython
echo.
echo   Python not found. Run the setup .bat first (it creates .venv).
goto :done

:done
echo.
pause
