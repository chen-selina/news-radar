@echo off
rem Double-click to open the website at http://127.0.0.1:8000
rem First run on a new computer: installs the Python packages and downloads the company list automatically.
rem If the website is already running, this just opens the browser. Close this window to stop the website.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set "PY=.venv\Scripts\python.exe"

curl -s -o nul http://127.0.0.1:8000/api/meta >nul 2>&1
if not errorlevel 1 (
  start "" http://127.0.0.1:8000
  exit /b 0
)

rem --- 1. Python environment. A .venv copied from another computer does not work, so it is rebuilt.
if exist "%PY%" "%PY%" -c "import fastapi, uvicorn, httpx, bs4" >nul 2>&1 && goto have_env
echo First-time setup [1/2]: installing Python packages. This takes a few minutes, please wait...
set "BASE="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE=py -3"
if not defined BASE python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE=python"
if not defined BASE goto no_python
if exist .venv rmdir /s /q .venv
%BASE% -m venv .venv || goto failed
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt || goto failed
:have_env

rem --- 2. Company list. Needs internet, takes about 10 seconds.
"%PY%" -c "import sys; from radar import db; sys.exit(0 if db.connect().execute('select count(*) from stocks').fetchone()[0] else 1)" >nul 2>&1 && goto have_data
echo First-time setup [2/2]: downloading the list of listed companies...
"%PY%" -m radar.stocks.load || goto failed
:have_data

rem --- 3. Start the website. The browser opens by itself once the website is ready.
echo.
"%PY%" -m radar.api --open %*
if errorlevel 1 goto failed
exit /b 0

:no_python
echo.
echo [ERROR] Python 3.11 or newer is not installed on this computer.
echo   1. Download it from https://www.python.org/downloads/
echo   2. In the installer, tick "Add python.exe to PATH"
echo   3. Double-click start_site.bat again
echo.
pause
exit /b 1

:failed
echo.
echo [ERROR] The website could not start. The reason is shown above.
echo If port 8000 is already in use, close the other program or run:  start_site.bat --port 8001
echo Otherwise please send a screenshot of this window to the team.
echo.
pause
exit /b 1
