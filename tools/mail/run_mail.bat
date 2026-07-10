@echo off
REM ============================================================
REM  AI-Elite Energy Dashboard - Energy Intensity Mail Sender
REM  (unified entry: daily / weekly / monthly)
REM  Usage:
REM    run_mail.bat daily                 (daily, default D-2)
REM    run_mail.bat daily 2026-07-08      (daily, specific date)
REM    run_mail.bat weekly                (weekly, last complete week)
REM    run_mail.bat monthly               (monthly, last complete month)
REM    run_mail.bat monthly 2026-06       (specific month)
REM    run_mail.bat weekly --dry-run      (build HTML only, no send)
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul

REM Move to project root (2 levels up from tools\mail)
pushd "%~dp0\..\.."
set "PROJECT_ROOT=%CD%"
echo [INFO] PROJECT_ROOT = %PROJECT_ROOT%

REM Auto-detect python: venv - python - py
set "PYEXE="
if exist "%PROJECT_ROOT%\venv\Scripts\python.exe" (
    set "PYEXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
) else if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set "PYEXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if !errorlevel! EQU 0 (
        set "PYEXE=python"
    ) else (
        where py >nul 2>&1
        if !errorlevel! EQU 0 (
            set "PYEXE=py -3"
        )
    )
)

if "%PYEXE%"=="" (
    echo [ERROR] Python executable not found. Check venv or system PATH.
    popd
    exit /b 10
)

echo [INFO] Python = %PYEXE%
echo [INFO] Run: tools\mail\run_mail.py %*
echo.

%PYEXE% "%PROJECT_ROOT%\tools\mail\run_mail.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [SUCCESS] Mail job finished
) else (
    echo [FAIL] Exit code %RC% - check logs\automation\*_mail_*.log
)

popd
endlocal & exit /b %RC%
