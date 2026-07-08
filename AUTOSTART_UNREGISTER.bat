@echo off
chcp 65001 >nul 2>&1
title BEMS - Unregister Auto-Start

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Administrator rights required. Right-click - "Run as administrator".
    echo.
    pause
    exit /b 1
)

schtasks /Delete /TN "BEMS_Server" /F
echo.
echo Auto-start has been unregistered.
echo (If a server window is currently running, close it manually.)
echo.
pause
