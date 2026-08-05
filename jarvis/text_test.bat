@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
set /p JARVIS_TEXT=Type a test request: 
".venv\Scripts\python.exe" jarvis.py --text "%JARVIS_TEXT%"
echo.
pause
