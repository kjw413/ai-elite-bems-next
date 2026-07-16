@echo off
chcp 65001 >nul 2>&1
title BEMS Next - Register Auto-Start

REM 로그온 시 RUN_BEMS_NEXT.bat 자동 실행 작업을 등록한다. 관리자 권한 필요, 1회 실행.

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator rights required. Right-click - "Run as administrator".
    pause & exit /b 1
)

set "LAUNCHER=%~dp0RUN_BEMS_NEXT.bat"

echo Target  : %LAUNCHER%
echo Task    : BEMS_Next_Server
echo Trigger : at logon
echo URL     : http://%COMPUTERNAME%:3000
echo.

schtasks /Create /TN "BEMS_Next_Server" /TR "\"%LAUNCHER%\"" /SC ONLOGON /IT /F
if errorlevel 1 ( echo [ERROR] Task registration failed. & pause & exit /b 1 )

echo.
echo Done. Auto-starts from the next logon.
echo   - Start now : schtasks /Run /TN "BEMS_Next_Server"
echo   - Disable   : AUTOSTART_UNREGISTER.bat
pause
