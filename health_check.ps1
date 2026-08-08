# BingX 실거래봇 헬스체크 - watchdog이 죽어있으면 자동 재시작
# CommandLine 문자열매칭(폴더명 포함 여부) 대신, watchdog.py가 실제로 잡는
# 전역 뮤텍스 존재 여부로 판단 -> 실행 방식(상대/절대경로 등)에 관계없이 정확함
$dir = $PSScriptRoot
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }

$MUTEX_NAME = "Global\BingX_Phoenix_Live_v1"

$running = $true
try {
    $m = [System.Threading.Mutex]::OpenExisting($MUTEX_NAME)
    $m.Close()
} catch {
    $running = $false
}

if (-not $running) {
    $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [HEALTH] watchdog 미실행 감지(뮤텍스 없음) -> 재시작"
    Add-Content -Path "$dir\watchdog.log" -Value $msg -Encoding UTF8

    try {
        Start-Process powershell.exe `
            -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\start_silent.ps1`"" `
            -WindowStyle Hidden
    } catch {
        Add-Content -Path "$dir\watchdog.log" -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [HEALTH][ERROR] 재시작 프로세스 기동 실패: $($_.Exception.Message)" -Encoding UTF8
    }
}
