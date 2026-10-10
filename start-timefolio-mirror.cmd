@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment missing. Set up the project first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -m backend.timefolio_mirror run
pause
