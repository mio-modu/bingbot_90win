# BingX 불사조봇 헬스체크 - watchdog이 죽어있으면 자동 재시작
$dir = $PSScriptRoot
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }

# watchdog.py가 실행 중인지 CommandLine 확인
$running = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
           Where-Object { $_.CommandLine -like '*Bingx_bot_certain*watchdog*' }

if (-not $running) {
    $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [HEALTH] watchdog 미실행 감지 -> 재시작"
    Add-Content -Path "$dir\watchdog.log" -Value $msg -Encoding UTF8

    Start-Process powershell.exe `
        -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\start_silent.ps1`"" `
        -WindowStyle Hidden
}
