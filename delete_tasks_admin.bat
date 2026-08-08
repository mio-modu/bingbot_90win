@echo off
echo Deleting BingX scheduled tasks...
schtasks /delete /tn "BingX_HealthCheck" /f
schtasks /delete /tn "BingX_PhoenixBot" /f
echo Done. Tasks deleted.
pause