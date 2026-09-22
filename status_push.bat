@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ─────────────────────────────────────────────────────────
REM  6개 모의봇 상태를 모아 GitHub 에 푸시
REM  BOT_ROOT = 봇 폴더 6개가 들어있는 상위 폴더
REM  경로가 바뀌면 아래 한 줄만 수정하면 된다.
REM ─────────────────────────────────────────────────────────
set "BOT_ROOT=C:\Users\psen7\OneDrive\바탕 화면\영상자동화"

python "%~dp0bot_status.py" --roots "%BOT_ROOT%" --expect 6 --stale-min 15 --push

if %errorlevel% neq 0 (
    echo.
    echo [실패] 상태 스냅샷 생성 또는 푸시에 실패했습니다.
    echo   - BOT_ROOT 경로가 맞는지 확인하세요: %BOT_ROOT%
    echo   - git 인증이 설정돼 있는지 확인하세요.
)
