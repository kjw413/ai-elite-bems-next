@echo off
REM Energy Intensity Mail - remove scheduled tasks
setlocal
chcp 65001 >nul

schtasks /Delete /TN "FEMS_Mail_Daily"   /F
schtasks /Delete /TN "FEMS_Mail_Weekly"  /F
schtasks /Delete /TN "FEMS_Mail_Monthly" /F

echo.
echo [DONE] Mail tasks removed (errors for non-existing tasks can be ignored).
pause
exit /b 0
