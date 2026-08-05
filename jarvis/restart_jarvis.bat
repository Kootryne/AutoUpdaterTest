@echo off
setlocal
cd /d "%~dp0"
call stop_jarvis.bat
timeout /t 1 /nobreak >nul
wscript.exe "%~dp0start_jarvis.vbs"
