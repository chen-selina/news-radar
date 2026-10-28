@echo off
rem Register two daily Windows tasks that run run_jobs.bat at 08:00 and 14:30 (computer clock).
rem Run again any time; existing tasks with the same name are replaced.
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not exist "%ROOT%\run_jobs.bat" (
  echo Cannot find "%ROOT%\run_jobs.bat"
  exit /b 1
)
schtasks /create /f /sc daily /st 08:00 /tn "TWNewsRadar 0800" /tr "\"%ROOT%\run_jobs.bat\""
if errorlevel 1 exit /b 1
schtasks /create /f /sc daily /st 14:30 /tn "TWNewsRadar 1430" /tr "\"%ROOT%\run_jobs.bat\""
if errorlevel 1 exit /b 1
echo.
echo Done. Tasks "TWNewsRadar 0800" and "TWNewsRadar 1430" will run "%ROOT%\run_jobs.bat".
echo Check them with:  schtasks /query /tn "TWNewsRadar 0800"
