@echo off
chcp 65001 >nul 2>&1
title BEMS Next (React UI :3000 / FastAPI :8000)
cd /d "%~dp0"

set "PYTHONDONTWRITEBYTECODE=1"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] new\.venv not found. Run SETUP_LOCAL.bat first.
    pause & exit /b 1
)
if not exist "backend\app\services" (
    echo [ERROR] Local core copy not found: backend\app
    pause & exit /b 1
)
where npm >nul 2>&1
if errorlevel 1 ( echo [ERROR] npm not found. Run SETUP_LOCAL.bat first. & pause & exit /b 1 )
where curl.exe >nul 2>&1
if errorlevel 1 ( echo [ERROR] curl.exe is required for the API readiness check. & pause & exit /b 1 )
netstat -ano 2>nul | findstr ":3000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] Port 3000 is already occupied. Stop the existing web process first.
    pause & exit /b 1
)

REM --- Build current source unless an operator explicitly reuses a verified bundle ---
if /I "%BEMS_SKIP_BUILD%"=="1" (
    if not exist ".next\BUILD_ID" (
        echo [ERROR] BEMS_SKIP_BUILD=1 but no production bundle exists.
        pause & exit /b 1
    )
    echo [NOTICE] Reusing the existing production bundle by BEMS_SKIP_BUILD=1.
) else (
    echo [BEMS] Building the current production source...
    call npm run build
    if errorlevel 1 ( echo [ERROR] next build failed. & pause & exit /b 1 )
)

REM --- FastAPI bridge (:8000), isolated new venv 사용 ---
netstat -ano 2>nul | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 goto start_api
curl.exe --fail --silent http://127.0.0.1:8000/api/v1/session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Port 8000 is occupied by a process that is not the BEMS API.
    pause & exit /b 1
)
echo [NOTICE] BEMS API already running on :8000 - skipping.
goto api_ready

:start_api
start "BEMS API :8000" "%~dp0.venv\Scripts\python.exe" -B -m uvicorn backend.server:app --host 0.0.0.0 --port 8000
set /a API_TRIES=0
:wait_api
curl.exe --fail --silent http://127.0.0.1:8000/api/v1/session >nul 2>&1
if not errorlevel 1 goto api_ready
set /a API_TRIES+=1
if %API_TRIES% GEQ 20 (
    echo [ERROR] BEMS API did not become ready within 20 seconds.
    pause & exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait_api

:api_ready

REM --- Next.js UI (:3000) ---
echo.
echo ============================================================
echo   BEMS Next running
echo   UI  : http://%COMPUTERNAME%:3000
echo   API : http://%COMPUTERNAME%:8000/api/docs
echo ============================================================
call npm run start
if errorlevel 1 (
    echo [ERROR] BEMS web server stopped with an error.
    pause & exit /b 1
)
