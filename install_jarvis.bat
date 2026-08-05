@echo off
setlocal
title Jarvis Installer

set "UPDATER=%TEMP%\jarvis_installer_%RANDOM%_%RANDOM%.ps1"
set "UPDATER_URL=https://raw.githubusercontent.com/Kootryne/AutoUpdaterTest/main/jarvis/installer.ps1"

echo Downloading the Jarvis installer from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -UseBasicParsing -Uri '%UPDATER_URL%' -OutFile '%UPDATER%'"

if errorlevel 1 (
    echo.
    echo Could not download the installer from GitHub.
    echo Check the internet connection and try again.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%UPDATER%"
set "RESULT=%ERRORLEVEL%"
del /q "%UPDATER%" >nul 2>nul

echo.
if not "%RESULT%"=="0" (
    echo Installation failed.
) else (
    echo Installation finished.
)
pause
exit /b %RESULT%
