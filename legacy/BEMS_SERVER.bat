@echo off
chcp 65001 >nul 2>&1
title BEMS Server (auto-start)

cd /d "%~dp0"

REM ============================================================
REM  BEMS always-on server launcher (headless / auto-restart)
REM  - Register via AUTOSTART_REGISTER.bat so it starts at logon.
REM  - Fixed URL:  http://%COMPUTERNAME%:8501
REM  - Close this window (or Ctrl+C) to stop the server.
REM ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run SETUP.bat first.
    pause
    exit /b 1
)

REM --- Prevent double launch if 8501 is already serving ---
netstat -ano 2>nul | findstr ":8501 " | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [NOTICE] BEMS is already running on port 8501. Closing this window.
    timeout /t 3 >nul
    exit /b 0
)

if not exist "logs\server" mkdir "logs\server"

REM --- Wait for MySQL (avoids boot-time race; up to ~60s) ---
echo [BEMS] Waiting for MySQL...
set /a _tries=0
:waitdb
"%~dp0.venv\Scripts\python.exe" -c "import sys;sys.path.insert(0,'.');from dotenv import load_dotenv;load_dotenv();import os,socket;s=socket.socket();s.settimeout(2);s.connect(('127.0.0.1',int(os.getenv('DB_PORT','3306'))));s.close()" >nul 2>&1
if %errorlevel% equ 0 goto dbok
set /a _tries+=1
if %_tries% geq 30 (
    echo [WARN] Could not confirm MySQL - continuing anyway.
    goto dbok
)
timeout /t 2 >nul
goto waitdb
:dbok

echo.
echo ============================================================
echo   BEMS server starting
echo   Fixed URL:  http://%COMPUTERNAME%:8501
echo   To stop: close this window or press Ctrl+C
echo ============================================================
echo.

REM --- Server loop: auto-restart 5s after any abnormal exit ---
:loop
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
echo [BEMS] Launching Streamlit (0.0.0.0:8501) ... log: logs\server\bems_server_%TODAY%.log
"%~dp0.venv\Scripts\python.exe" -m streamlit run app/main.py --server.address 0.0.0.0 --server.port 8501 --server.headless true --browser.gatherUsageStats false >> "logs\server\bems_server_%TODAY%.log" 2>&1
echo [BEMS] Streamlit exited. Restarting in 5s... (close this window to stop for good)
timeout /t 5 >nul
goto loop
