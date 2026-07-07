@echo off
chcp 65001 >nul 2>&1
title FEMS - Factory Energy Management System

echo.
echo ============================================================
echo   FEMS - Factory Energy Management System
echo ============================================================
echo.

cd /d "%~dp0"

REM ============================================================
REM  Check virtual environment
REM ============================================================
if not exist ".venv\Scripts\activate.bat" (
    echo [NOTICE] Virtual environment not found.
    echo          Please run SETUP.bat first.
    echo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

REM ============================================================
REM  STEP 1: Quick package check & repair
REM ============================================================
echo [1/3] Checking packages...
"%~dp0.venv\Scripts\python.exe" -c "import streamlit; import mysql.connector; import pandas; import plotly; import openpyxl; import xlrd; import dotenv"
if %errorlevel% neq 0 (
    echo.
    echo [NOTICE] Some packages are missing or venv is inconsistent.
    echo          Attempting repair using pip...
    "%~dp0.venv\Scripts\python.exe" -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo.
        echo [ERROR] Package installation/repair failed.
        pause
        exit /b 1
    )
)
echo         Packages OK!

REM ============================================================
REM  STEP 2: Initialize database
REM ============================================================
echo.
echo [2/3] Initializing database...
"%~dp0.venv\Scripts\python.exe" -c "import sys; sys.path.insert(0,'.'); from app.database.db_connection import init_db; init_db()"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Database initialization failed.
    echo         Please check if MySQL service is running and .env is correct.
    pause
    exit /b 1
)
echo         DB ready!

REM ============================================================
REM  STEP 3: Check if already running and start server
REM ============================================================
echo.
echo [3/3] Checking if system is already running...

set PORT=8501

netstat -ano 2>nul | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo   [NOTICE] FEMS is already running on port %PORT%.
    echo   Please use the existing window or access:
    echo   URL: http://localhost:%PORT%
    echo ============================================================
    echo.
    pause
    exit /b 0
)

echo         Port %PORT% is available. Starting server...
echo.
echo ============================================================
echo   URL: http://localhost:%PORT%
echo   To stop: close this window or press Ctrl+C
echo ============================================================
echo.
echo   Environment info:
"%~dp0.venv\Scripts\python.exe" -c "import sys; print('   Python:', sys.executable)"
echo   Project: %~dp0
echo   Port   : %PORT%
echo.

start "" cmd /c "timeout /t 5 >nul & start http://localhost:%PORT%"

"%~dp0.venv\Scripts\python.exe" -m streamlit run app/main.py --server.address 0.0.0.0 --server.port %PORT% --server.headless true --browser.gatherUsageStats false

pause
