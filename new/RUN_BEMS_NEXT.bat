@echo off
chcp 65001 >nul 2>&1
title BEMS Next (React UI :3000 / FastAPI :8000)
cd /d "%~dp0"

if "%BEMS_CORE_ROOT%"=="" set BEMS_CORE_ROOT=%~dp0..\legacy
if not exist "%BEMS_CORE_ROOT%\.venv\Scripts\python.exe" (
    echo [ERROR] legacy .venv not found at %BEMS_CORE_ROOT%. Run SETUP_LOCAL.bat first.
    pause & exit /b 1
)

REM --- FastAPI bridge (:8000), legacy venv 사용 ---
netstat -ano 2>nul | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [NOTICE] API already running on :8000 - skipping.
) else (
    start "BEMS API :8000" cmd /c ""%BEMS_CORE_ROOT%\.venv\Scripts\python.exe" -m uvicorn backend.server:app --host 0.0.0.0 --port 8000"
)

REM --- Next.js UI (:3000) ---
if not exist ".next\BUILD_ID" (
    echo [BEMS] First run - building production bundle...
    call npm run build
    if %errorlevel% neq 0 ( echo [ERROR] next build failed. & pause & exit /b 1 )
)

echo.
echo ============================================================
echo   BEMS Next running
echo   UI  : http://%COMPUTERNAME%:3000
echo   API : http://%COMPUTERNAME%:8000/api/docs
echo ============================================================
call npm run start
