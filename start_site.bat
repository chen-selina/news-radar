@echo off
rem Double-click to open the website at http://127.0.0.1:8000
rem If the website is already running, just open the browser. Otherwise start it (close this window to stop).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
curl -s -o nul http://127.0.0.1:8000/api/meta >nul 2>&1
if not errorlevel 1 (
  start http://127.0.0.1:8000
  exit /b 0
)
rem open the browser 3 seconds later, after the server has started
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m radar.api %*
