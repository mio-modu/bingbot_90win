@echo off
cd /d "%~dp0"
echo Closing all positions and resetting state...
python close_all.py
if errorlevel 1 (
    echo.
    echo [FAILED] Close failed. Check exchange manually, then run run_hidden.vbs
    pause
    exit /b 1
)
echo.
echo Starting bot in background...
wscript run_hidden.vbs
echo Bot started. This window will close.
timeout /t 3 /nobreak >nul