@echo off
REM ============================================================
REM  Energy Intensity Mail - Windows Task Scheduler registration
REM  (host PC only - tasks run under the current login account,
REM   so only the admin can send mail)
REM
REM  Registered tasks:
REM    FEMS_Mail_Daily   : every day        14:00  (D-2 data, after RPA noon run)
REM    FEMS_Mail_Weekly  : every Tuesday    14:10  (last complete Mon-Sun week)
REM    FEMS_Mail_Monthly : day 3 of month   14:20  (last complete month)
REM
REM  Why Tuesday for weekly: MIS RPA collects up to D-2, so on Monday
REM    the last Sunday's data is not yet available.
REM  Why day 3 for monthly: last day of month (D-2) is collected on day 2.
REM
REM  To change times: edit /ST values below and re-run (/F overwrites).
REM  To remove: UNREGISTER_MAIL_SCHEDULE.bat
REM ============================================================

setlocal
chcp 65001 >nul

set "RUNNER=%~dp0run_mail.bat"

echo [INFO] Runner: %RUNNER%
echo.

schtasks /Create /TN "FEMS_Mail_Daily"   /TR "\"%RUNNER%\" daily"   /SC DAILY   /ST 14:00 /F
if %errorlevel% neq 0 goto :fail

schtasks /Create /TN "FEMS_Mail_Weekly"  /TR "\"%RUNNER%\" weekly"  /SC WEEKLY  /D TUE /ST 14:10 /F
if %errorlevel% neq 0 goto :fail

schtasks /Create /TN "FEMS_Mail_Monthly" /TR "\"%RUNNER%\" monthly" /SC MONTHLY /D 3 /ST 14:20 /F
if %errorlevel% neq 0 goto :fail

echo.
echo ============================================================
echo   [SUCCESS] 3 tasks registered
echo     - FEMS_Mail_Daily   : every day 14:00
echo     - FEMS_Mail_Weekly  : every Tue 14:10
echo     - FEMS_Mail_Monthly : day 3     14:20
echo   Test now:  schtasks /Run /TN "FEMS_Mail_Daily"
echo   Remove:    UNREGISTER_MAIL_SCHEDULE.bat
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Task registration failed - try running as administrator.
pause
exit /b 1
