@echo off
chcp 65001 >nul
:: 관리자 권한 확인 → 없으면 자동 요청
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 관리자 권한으로 다시 실행합니다...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo  [BingX Live 자동시작 등록 중...]
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0_register_tasks.ps1"
echo.
pause
