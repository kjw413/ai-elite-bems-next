@echo off
chcp 65001 >nul 2>&1
title BEMS - Register Auto-Start

REM ============================================================
REM  Registers a scheduled task so BEMS_SERVER.bat auto-starts
REM  at logon. Requires administrator rights. Run once.
REM ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Administrator rights required.
    echo         Right-click this file - "Run as administrator".
    echo.
    pause
    exit /b 1
)

set "LAUNCHER=%~dp0BEMS_SERVER.bat"

echo Target  : %LAUNCHER%
echo Task    : BEMS_Server
echo Trigger : at logon
echo URL     : http://%COMPUTERNAME%:8501
echo.

schtasks /Create /TN "BEMS_Server" /TR "\"%LAUNCHER%\"" /SC ONLOGON /IT /F
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Task registration failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done!
echo   - Server auto-starts from your next logon.
echo   - To start now without reboot:
echo       schtasks /Run /TN "BEMS_Server"
echo   - Fixed URL:  http://%COMPUTERNAME%:8501
echo   - To disable auto-start, run AUTOSTART_UNREGISTER.bat
echo ============================================================
echo.
pause
