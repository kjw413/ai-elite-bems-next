@echo off
chcp 65001 >nul 2>&1
title BEMS Next - Setup (Run Once)
cd /d "%~dp0"

REM legacy(.venv) 위에 FastAPI 의존성 설치 + Node 패키지 설치
if "%BEMS_CORE_ROOT%"=="" set BEMS_CORE_ROOT=%~dp0..\legacy

echo [1/3] Checking legacy venv: %BEMS_CORE_ROOT%\.venv
if not exist "%BEMS_CORE_ROOT%\.venv\Scripts\python.exe" (
    echo [ERROR] legacy .venv not found. Run legacy SETUP.bat first.
    pause & exit /b 1
)

echo [2/3] Installing FastAPI bridge packages into legacy venv...
"%BEMS_CORE_ROOT%\.venv\Scripts\python.exe" -m pip install -r backend\requirements.txt --quiet
if %errorlevel% neq 0 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

echo [3/3] Installing Node packages (npm install)...
where npm >nul 2>&1
if %errorlevel% neq 0 ( echo [ERROR] Node.js 22.13+ required. & pause & exit /b 1 )
call npm install
if %errorlevel% neq 0 ( echo [ERROR] npm install failed. & pause & exit /b 1 )

echo.
echo Setup complete. Next: run CONFIGURE_FIREWALL.bat (admin) once, then RUN_BEMS_NEXT.bat
pause
