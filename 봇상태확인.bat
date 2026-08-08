@echo off
chcp 65001 >nul
powershell -NoExit -Command "& { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Content -Path '%~dp0bot.log' -Wait -Tail 30 -Encoding UTF8 }"
