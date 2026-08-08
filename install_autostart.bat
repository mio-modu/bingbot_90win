@echo off
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
copy /Y "%~dp0run_hidden.vbs" "%STARTUP%\bingx_bot.vbs" >nul
echo Autostart OK - Windows login time bot starts automatically.
pause