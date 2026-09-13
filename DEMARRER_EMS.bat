@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -m pip install -r requirements.txt
  py start_ems.py
) else (
  python -m pip install -r requirements.txt
  python start_ems.py
)
pause
