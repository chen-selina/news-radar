@echo off
rem Run a project command with the project's own Python (.venv), so all packages are found.
rem Usage:  run train            run backfill --status            run compare --backfill
rem This is the same as:  .venv\Scripts\python.exe -m radar.train   (and so on)
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Please double-click start_site.bat once first. It sets up Python packages and company data.
  exit /b 1
)
if "%~1"=="" (
  echo Usage: run ^<command^> [options]
  echo   run train                 estimate term weights
  echo   run backfill --status     show backfill progress
  echo   run compare --backfill    compare the three dictionaries
  echo   run validate --dedupe     validate predictions
  exit /b 1
)
set "CMD=%~1"
shift
set "ARGS="
:collect
if "%~1"=="" goto go
set "ARGS=%ARGS% %1"
shift
goto collect
:go
if /i "%CMD%"=="train" "%PY%" -c "import sklearn" 2>nul || "%PY%" -m pip install --quiet --disable-pip-version-check -r requirements-ml.txt
"%PY%" -m radar.%CMD%%ARGS%
