@echo off
chcp 65001 >nul 2>&1
title BEMS Next - API Status Check
cd /d "%~dp0"

echo ============================================================
echo   BEMS API (:8000) Status
echo ============================================================
echo.

set "PID="
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do set "PID=%%p"

if not defined PID (
    echo [STOPPED] Nothing is listening on port 8000.
    goto :check_ui
)

echo [PORT] 8000 is held by PID %PID%.

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [NOTICE] curl.exe not found - cannot verify this is the BEMS API.
    goto :check_ui
)

curl.exe --fail --silent --max-time 3 http://127.0.0.1:8000/api/v1/session >nul 2>&1
if errorlevel 1 (
    echo [WARNING] PID %PID% is not responding as the BEMS API. It may be a different process.
) else (
    echo [RUNNING] BEMS API is responding normally.
    echo           Docs   : http://%COMPUTERNAME%:8000/api/docs
    echo           Health : http://%COMPUTERNAME%:8000/api/v1/health
)

:check_ui
echo.
netstat -ano 2>nul | findstr ":3000 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [UI STOPPED] Nothing is listening on port 3000.
) else (
    echo [UI RUNNING] http://%COMPUTERNAME%:3000
)

echo.
pause
