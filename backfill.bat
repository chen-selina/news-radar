@echo off
rem Backfill past news (links from FinMind + article text). Safe to stop with Ctrl+C; run again to continue.
rem Default: top 20 stocks by market cap, last 90 days. Extra options are passed through, e.g. backfill.bat --status
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Please double-click start_site.bat once first. It sets up Python packages and company data.
  pause
  exit /b 1
)
if "%~1"=="" (
  "%PY%" -m radar.backfill --top 20 --days 90
) else (
  "%PY%" -m radar.backfill %*
)
echo.
echo Finished or stopped. Run this file again to continue where it left off.
pause
