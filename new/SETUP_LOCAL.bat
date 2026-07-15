@echo off
chcp 65001 >nul 2>&1
title BEMS Next - Setup (Run Once)
cd /d "%~dp0"

REM legacy 소스는 읽기 전용으로 두고 new 전용 .venv와 Node 패키지를 설치
set "PYTHONDONTWRITEBYTECODE=1"
if not defined BEMS_CORE_ROOT set "BEMS_CORE_ROOT=%~dp0..\legacy"

echo [1/5] Checking read-only legacy core: "%BEMS_CORE_ROOT%"
if not exist "%BEMS_CORE_ROOT%\requirements.txt" (
    echo [ERROR] legacy requirements.txt not found: %BEMS_CORE_ROOT%
    pause & exit /b 1
)

set "PYTHON_EXE=%BEMS_CORE_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=python"
    ) else (
        where py >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python not found. Install a legacy-compatible Python first.
            pause & exit /b 1
        )
        set "PYTHON_EXE=py"
    )
)

echo [2/5] Creating isolated new\.venv...
if not exist ".venv\Scripts\python.exe" (
    "%PYTHON_EXE%" -m venv .venv
    if errorlevel 1 ( echo [ERROR] new .venv creation failed. & pause & exit /b 1 )
)

echo [3/5] Installing legacy runtime and FastAPI bridge packages into new\.venv...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if errorlevel 1 ( echo [ERROR] pip upgrade failed. & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install -r "%BEMS_CORE_ROOT%\requirements.txt" -r backend\requirements.txt --quiet
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

echo [4/5] Installing locked Node packages...
where npm >nul 2>&1
if errorlevel 1 ( echo [ERROR] Node.js 22.13+ required. & pause & exit /b 1 )
set "NODE_MAJOR="
set "NODE_MINOR="
for /f "tokens=1,2 delims=." %%A in ('node -p "process.versions.node" 2^>nul') do (
    set "NODE_MAJOR=%%A"
    set "NODE_MINOR=%%B"
)
if not defined NODE_MAJOR ( echo [ERROR] Unable to detect Node.js version. & pause & exit /b 1 )
if %NODE_MAJOR% LSS 22 ( echo [ERROR] Node.js 22.13+ required. & pause & exit /b 1 )
if %NODE_MAJOR% EQU 22 if %NODE_MINOR% LSS 13 ( echo [ERROR] Node.js 22.13+ required. & pause & exit /b 1 )
if not exist "package-lock.json" (
    echo [ERROR] package-lock.json is required for a reproducible install.
    pause & exit /b 1
)
call npm ci --no-audit --no-fund
if errorlevel 1 ( echo [ERROR] npm ci failed. & pause & exit /b 1 )

echo [5/5] Verifying the production build...
call npm run build
if errorlevel 1 ( echo [ERROR] next build failed. & pause & exit /b 1 )

echo.
echo Setup complete. Next: run CONFIGURE_FIREWALL.bat (admin) once, then RUN_BEMS_NEXT.bat
pause
