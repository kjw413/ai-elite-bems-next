@echo off
chcp 65001 >nul 2>&1
title BEMS Next - Unregister Auto-Start

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator rights required. Right-click - "Run as administrator".
    pause & exit /b 1
)

schtasks /Delete /TN "BEMS_Next_Server" /F
if errorlevel 1 ( echo [NOTICE] Task not found or already removed. )

echo Done. Auto-start disabled.
pause
