@echo off
rem Remove the two scheduled tasks created by schedule_windows.bat
schtasks /delete /f /tn "TWNewsRadar 0800"
schtasks /delete /f /tn "TWNewsRadar 1430"
