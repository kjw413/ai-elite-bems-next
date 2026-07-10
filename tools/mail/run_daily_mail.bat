@echo off
REM ============================================================
REM  AI-Elite Energy Dashboard - 일일 생산량·에너지 이상 Alert 자동 송부
REM  사용:
REM    run_daily_mail.bat                 (기본: D-2 자동)
REM    run_daily_mail.bat 2026-05-09      (특정 일자)
REM    run_daily_mail.bat --dry-run       (실제 발송 없이 HTML만 저장)
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul

REM 프로젝트 루트로 이동 (tools\mail 의 상위 2단계)
pushd "%~dp0\..\.."
set "PROJECT_ROOT=%CD%"
echo [INFO] PROJECT_ROOT = %PROJECT_ROOT%

REM Python 실행파일 자동 탐색: venv → python → py
set "PYEXE="
if exist "%PROJECT_ROOT%\venv\Scripts\python.exe" (
    set "PYEXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
) else if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set "PYEXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if !errorlevel! EQU 0 (
        set "PYEXE=python"
    ) else (
        where py >nul 2>&1
        if !errorlevel! EQU 0 (
            set "PYEXE=py -3"
        )
    )
)

if "%PYEXE%"=="" (
    echo [ERROR] Python 실행파일을 찾을 수 없습니다. venv 또는 시스템 PATH를 확인하세요.
    popd
    exit /b 10
)

echo [INFO] Python = %PYEXE%
echo [INFO] 실행: tools\mail\run_daily_mail.py %*
echo.

%PYEXE% "%PROJECT_ROOT%\tools\mail\run_daily_mail.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [SUCCESS] 메일 송부 작업 완료
) else (
    echo [FAIL] 종료 코드 %RC% - logs\automation\daily_mail_*.log 를 확인하세요.
)

popd
endlocal & exit /b %RC%
