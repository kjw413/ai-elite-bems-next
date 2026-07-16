@echo off
chcp 65001 >nul 2>&1
title BEMS Next - Setup (Run Once)
cd /d "%~dp0"

REM Self-contained setup: everything installs under new\ (no legacy dependency).
set "PYTHONDONTWRITEBYTECODE=1"

echo [1/5] Checking local core copy and requirements...
if not exist "backend\app\services" (
    echo [ERROR] backend\app core copy not found. See docs\AI_Elite_BEMS_Next_독립화_계획서.md
    pause & exit /b 1
)
if not exist "backend\requirements-core.txt" (
    echo [ERROR] backend\requirements-core.txt not found.
    pause & exit /b 1
)

where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=python"
) else (
    where py >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.11+ first.
        pause & exit /b 1
    )
    set "PYTHON_EXE=py"
)

echo [2/5] Creating isolated new\.venv...
if not exist ".venv\Scripts\python.exe" (
    "%PYTHON_EXE%" -m venv .venv
    if errorlevel 1 ( echo [ERROR] new .venv creation failed. & pause & exit /b 1 )
)

echo [3/5] Installing core runtime and FastAPI bridge packages into new\.venv...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if errorlevel 1 ( echo [ERROR] pip upgrade failed. & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install -r backend\requirements-core.txt -r backend\requirements.txt --quiet
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
