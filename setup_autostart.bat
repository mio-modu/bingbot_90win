@echo off
chcp 65001 >nul 2>&1
set TASK_NAME=BingX_PhoenixBot2
set HEALTH_TASK=BingX_HealthCheck2

for /f "tokens=*" %%i in ('where python') do (
    set PYTHON_EXE=%%i
    goto :found
)
:found

echo.
echo  [BingX 불사조봇 Live] 자동시작 등록
echo  파이썬  : %PYTHON_EXE%
echo  태스크  : %TASK_NAME%, %HEALTH_TASK%
echo.

:: 기존 태스크 삭제 후 재등록
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
schtasks /delete /tn "%HEALTH_TASK%" /f >nul 2>&1

:: ① 로그인 시 자동 시작
schtasks /create /tn "%TASK_NAME%" /tr "\"%PYTHON_EXE%\" \"%~dp0watchdog.py\" --auto" /sc onlogon /rl highest /f /delay 0000:30

:: ② 5분마다 헬스체크 — watchdog이 죽어있으면 재시작
set HEALTH_CMD=powershell -WindowStyle Hidden -Command "if (-not (Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*watchdog*' })) { Start-Process '%PYTHON_EXE%' -ArgumentList '\"%~dp0watchdog.py\" --auto' -WindowStyle Minimized }"
schtasks /create /tn "%HEALTH_TASK%" /tr "%HEALTH_CMD%" /sc minute /mo 5 /rl highest /f

if %errorlevel% == 0 (
    echo  [완료] 자동시작 등록 성공!
    echo.
    echo  적용 내용:
    echo    - PC 로그인 시 watchdog 자동 시작 (30초 후)
    echo    - 5분마다 watchdog 생존 확인 및 자동 재시작
    echo.
    echo  ※ 지금 바로 적용하려면 watchdog을 수동으로 한 번 실행하세요.
) else (
    echo  [실패] 오른쪽 클릭 - 관리자 권한으로 실행 후 다시 시도하세요.
)
echo.
pause
