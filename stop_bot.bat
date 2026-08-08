@echo off
taskkill /F /FI "WINDOWTITLE eq BingX*" 2>nul
FOR /F "tokens=2" %%i IN ('tasklist /FI "IMAGENAME eq python.exe" /NH 2^>nul') DO taskkill /F /PID %%i 2>nul
echo Bot stopped.