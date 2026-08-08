$python  = "C:\Users\psen7\AppData\Local\Programs\Python\Python313\python.exe"
$ps1     = "C:\Users\psen7\OneDrive\바탕 화면\영상자동화\Bingx_bot_live\start_silent.ps1"
$hcheck  = "C:\Users\psen7\OneDrive\바탕 화면\영상자동화\Bingx_bot_live\health_check.py"
$workdir = "C:\Users\psen7\OneDrive\바탕 화면\영상자동화\Bingx_bot_live"

# 재시작 비활성화: watchdog이 직접 재시작을 관리
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0

# ── 1. 로그인 시 봇 자동시작 ──────────────────────────────
$action1  = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`"" `
    -WorkingDirectory $workdir
$trigger1 = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "BingX_PhoenixBot2" `
    -Action $action1 -Trigger $trigger1 -Settings $settings `
    -RunLevel Highest -Force | Out-Null
Write-Host "  [OK] BingX_PhoenixBot2  (로그인 시 자동시작)" -ForegroundColor Green

# ── 2. 5분마다 헬스체크 (health_check.py 사용 - wmic 기반으로 정확하게 감지) ──
$action2  = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$hcheck`"" `
    -WorkingDirectory $workdir
$trigger2 = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) -Once -At (Get-Date)
Register-ScheduledTask -TaskName "BingX_HealthCheck2" `
    -Action $action2 -Trigger $trigger2 -Settings $settings `
    -RunLevel Highest -Force | Out-Null
Write-Host "  [OK] BingX_HealthCheck2 (5분마다 생존 확인)" -ForegroundColor Green

Write-Host ""
Write-Host "완료! PC를 껐다 켜도 봇이 자동으로 백그라운드 실행됩니다." -ForegroundColor Cyan
