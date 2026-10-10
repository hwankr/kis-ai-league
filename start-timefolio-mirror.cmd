@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment missing. Set up the project first.
    pause
    exit /b 1
)
if exist ".local\timefolio-chrome\chrome-win64\chrome.exe" (
    ".venv\Scripts\python.exe" -X utf8 -m backend.timefolio_mirror run --headless --browser-executable ".local\timefolio-chrome\chrome-win64\chrome.exe"
) else (
    ".venv\Scripts\python.exe" -X utf8 -m backend.timefolio_mirror run --headless
)
pause
