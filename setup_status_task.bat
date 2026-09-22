@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ─────────────────────────────────────────────────────────
REM  봇 상태 스냅샷을 5분마다 자동 실행하도록 등록
REM  (관리자 권한 불필요 — 현재 사용자 계정으로 등록)
REM ─────────────────────────────────────────────────────────
set "TASK_NAME=BotStatusPush"
set "RUNNER=%~dp0status_push.bat"

schtasks /create /tn "%TASK_NAME%" /tr "\"%RUNNER%\"" /sc minute /mo 5 /f >nul 2>&1

if %errorlevel% == 0 (
    echo ✔ 등록 완료: %TASK_NAME%  — 5분마다 상태를 GitHub 에 푸시합니다.
    echo.
    echo   지금 바로 한 번 실행:  schtasks /run /tn "%TASK_NAME%"
    echo   등록 해제:             schtasks /delete /tn "%TASK_NAME%" /f
) else (
    echo ✗ 등록 실패. 명령 프롬프트를 관리자 권한으로 실행해 보세요.
)
pause
