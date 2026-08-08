@echo off
chcp 65001 >nul 2>&1
set TASK_NAME=BingX_PhoenixBot2
set HEALTH_TASK=BingX_HealthCheck2

echo.
echo  [BingX 불사조봇 Live] 자동시작 제거
echo.
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
schtasks /delete /tn "%HEALTH_TASK%" /f >nul 2>&1

echo  [완료] 자동시작 작업 제거됨.
echo.
pause
