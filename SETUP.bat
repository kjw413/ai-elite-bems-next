@echo off
chcp 65001 >nul 2>&1
title BEMS - Setup (Run Once)

echo.
echo ============================================================
echo   BEMS - Binggrae Energy Management System
echo   Environment Setup  (Run this ONCE before first launch)
echo ============================================================
echo.

cd /d "%~dp0"

REM ============================================================
REM  STEP 1: Check Python
REM ============================================================
echo [1/4] Checking Python installation...

set PYTHON_CMD=python
where %PYTHON_CMD% >nul 2>&1
if %errorlevel% neq 0 (
    set PYTHON_CMD=py
    where %PYTHON_CMD% >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo [ERROR] Python not found.
        echo         Please install Python 3.8 or later:
        echo         https://www.python.org/downloads/
        echo         Make sure to check "Add Python to PATH" during install.
        echo.
        pause
        exit /b 1
    )
)

echo         Found Python using command: %PYTHON_CMD%

REM Get version
for /f "tokens=2" %%V in ('%PYTHON_CMD% --version 2^>^&1') do set PYVER=%%V
echo         Found Python %PYVER%

REM ============================================================
REM  STEP 2: Create virtual environment (.venv)
REM ============================================================
echo.
echo [2/4] Setting up virtual environment (.venv)...

if exist ".venv\Scripts\activate.bat" (
    echo         Existing .venv found. Reusing it.
) else (
    echo         Creating new .venv...
    %PYTHON_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo         .venv created!
)

REM ============================================================
REM  STEP 3: Install packages
REM ============================================================
echo.
echo [3/4] Installing required packages...
echo         (This may take a few minutes on first run.)
echo.

.\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.\.venv\Scripts\pip.exe install -r requirements.txt --quiet

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Package installation failed.
    pause
    exit /b 1
)

echo         Packages installed!

REM ============================================================
REM  STEP 4: Initialize database
REM ============================================================
echo.
echo [4/4] Initializing database...

.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from app.database.db_connection import init_db; init_db(); print('        DB initialized!')"

if %errorlevel% neq 0 (
    echo [ERROR] Database initialization failed.
    pause
    exit /b 1
)

REM ============================================================
REM  Done
REM ============================================================
echo.
echo ============================================================
echo   Setup complete!
echo ============================================================
echo.
echo   You can now launch the app by running:
echo     WEB 실행.bat
echo.
echo ============================================================
echo.
pause
