@echo off
rem Scheduled job: crawl news, then fetch closing prices.
rem Windows Task Scheduler runs this at 08:00 and 14:30 (see tools\schedule_windows.bat).
rem Output is also written to data\logs\jobs-YYYY-MM.log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem UTF-8 mode: Python and pip read text files as UTF-8 instead of the Windows code page (cp950)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m radar.jobs %*
