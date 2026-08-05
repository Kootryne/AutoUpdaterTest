@echo off
setlocal
cd /d "%~dp0"
title Connect Jarvis to GitHub
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_github.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo GitHub setup failed.
pause
exit /b %RESULT%
