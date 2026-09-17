@echo off
REM ============================================================
REM  AI Elite BEMS Next - Access Log Backfill
REM  Loads access records for the period before access logging existed.
REM  Talks to MySQL directly - the FastAPI server does not need to be running.
REM
REM  Usage:
REM    RUN_ACCESS_BACKFILL.bat                 load with defaults (May 1 .. yesterday)
REM    RUN_ACCESS_BACKFILL.bat --check         check DB and tables only, then stop
REM    RUN_ACCESS_BACKFILL.bat --from 2026-03-02 --max 20
REM      For every option: backend\tools\backfill_access_daily.py --help
REM
REM  Shows a dry-run preview first and loads only after you confirm.
REM  Days that already have records are skipped, so live counts are never lost.
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul 2>&1
title BEMS Next - Access Log Backfill
cd /d "%~dp0"

set "RC=0"
set "SCRIPT=backend\tools\backfill_access_daily.py"

if not exist "%SCRIPT%" (
    echo [ERROR] %SCRIPT% not found. Run this from the repository root.
    set "RC=1"
    goto :finish
)
if not exist "backend\.env" (
    echo [ERROR] backend\.env not found - no DB credentials to connect with.
    echo         Copy backend\.env from the existing server PC, then run again.
    set "RC=1"
    goto :finish
)

REM Auto-detect python: .venv (SETUP_LOCAL.bat) - python - py
set "PYEXE="
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if !errorlevel! EQU 0 (
        set "PYEXE=python"
    ) else (
        where py >nul 2>&1
        if !errorlevel! EQU 0 set "PYEXE=py -3"
    )
)
if "!PYEXE!"=="" (
    echo [ERROR] Python not found. Run SETUP_LOCAL.bat first.
    set "RC=10"
    goto :finish
)

set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
echo [INFO] Python = !PYEXE!
echo.

REM --check only inspects; skip the preview and confirmation steps.
echo.%*| findstr /C:"--check" >nul
if !errorlevel! EQU 0 goto :checkonly

echo ============================================================
echo  Step 1 of 2 - Preview ^(nothing is written^)
echo ============================================================
!PYEXE! "%SCRIPT%" --dry-run %*
if !errorlevel! NEQ 0 (
    echo.
    echo [FAIL] Stopped during preview. Check the messages above.
    set "RC=1"
    goto :finish
)

echo.
echo ============================================================
echo  Step 2 of 2 - Load
echo ============================================================
echo  The values shown above will be written to MySQL.
echo  Days that already have records are skipped.
echo.
set "CONFIRM="
set /p "CONFIRM=Type Y then Enter to load (just Enter to cancel): "
if /I not "!CONFIRM!"=="Y" (
    echo.
    echo Cancelled. Nothing was written.
    goto :finish
)

echo.
!PYEXE! "%SCRIPT%" %*
set "RC=!ERRORLEVEL!"

echo.
if "!RC!"=="0" (
    echo [SUCCESS] Load finished.
    echo           Check it in the admin menu: Admin ^> Access Stats tab.
) else (
    echo [FAIL] Exit code !RC! - check the messages above.
)
goto :finish

:checkonly
!PYEXE! "%SCRIPT%" %*
set "RC=!ERRORLEVEL!"

:finish
echo.
pause
endlocal & exit /b %RC%
