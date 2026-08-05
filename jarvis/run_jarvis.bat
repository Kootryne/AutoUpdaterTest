@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    call install.bat
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" jarvis.py
echo.
echo Jarvis exited. Press any key to close.
pause >nul
