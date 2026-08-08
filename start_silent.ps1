# 실거래봇 무창 기동 — 경로는 스크립트 위치에서 유도한다.
# (한글 경로를 하드코딩하면 .ps1 인코딩에 따라 경로가 깨지므로 절대 넣지 말 것)
$dir = $PSScriptRoot
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }

$python   = "C:\Users\psen7\AppData\Local\Programs\Python\Python313\python.exe"
$watchdog = Join-Path $dir "watchdog.py"

Start-Process -FilePath $python `
              -ArgumentList "`"$watchdog`" --auto" `
              -WorkingDirectory $dir `
              -WindowStyle Hidden
