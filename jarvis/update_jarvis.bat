@echo off
setlocal
title Jarvis Updater
cd /d "%~dp0"

set "UPDATER=%TEMP%\jarvis_updater_%RANDOM%_%RANDOM%.ps1"
set "UPDATER_URL=https://raw.githubusercontent.com/Kootryne/AutoUpdaterTest/main/jarvis/installer.ps1"

echo Checking GitHub for a Jarvis update...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -UseBasicParsing -Uri '%UPDATER_URL%' -OutFile '%UPDATER%'"

if errorlevel 1 (
    echo.
    echo Could not download the updater from GitHub.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%UPDATER%" -UpdateOnly
set "RESULT=%ERRORLEVEL%"
del /q "%UPDATER%" >nul 2>nul

echo.
pause
exit /b %RESULT%
