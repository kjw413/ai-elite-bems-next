@echo off
title BEMS Next - Firewall (Run as Administrator, Once)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Run this file as Administrator.
    pause & exit /b 1
)

netsh advfirewall firewall delete rule name="BEMS Next UI 3000" >nul 2>&1
netsh advfirewall firewall add rule name="BEMS Next UI 3000" dir=in action=allow protocol=TCP localport=3000 profile=domain,private
netsh advfirewall firewall delete rule name="BEMS Next API 8000" >nul 2>&1
netsh advfirewall firewall add rule name="BEMS Next API 8000" dir=in action=allow protocol=TCP localport=8000 profile=domain,private

echo Firewall rules added for TCP 3000 / 8000 (domain, private profiles).
pause
