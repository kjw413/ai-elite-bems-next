@echo off
chcp 65001 >nul 2>&1
title BEMS Next - Stop API
cd /d "%~dp0"

set "PID="
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do set "PID=%%p"

if not defined PID (
    echo [NOTICE] Nothing is listening on port 8000. Already stopped.
    pause & exit /b 0
)

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] curl.exe not found - cannot verify PID %PID% is the BEMS API.
    echo         Refusing to kill it blindly. Check Task Manager manually.
    pause & exit /b 1
)

curl.exe --fail --silent --max-time 3 http://127.0.0.1:8000/api/v1/session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PID %PID% does not look like the BEMS API.
    echo         Refusing to kill it - check Task Manager manually.
    pause & exit /b 1
)

echo Stopping BEMS API (PID %PID%)...
taskkill /PID %PID% /F >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to stop the process. Try running this as Administrator.
    pause & exit /b 1
)

echo Done. Run RUN_BEMS_NEXT.bat to start it again.
pause
