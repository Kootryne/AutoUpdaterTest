@echo off
setlocal
cd /d "%~dp0"
set "TEMP_HOST=%TEMP%\jarvis_console_host_%RANDOM%_%RANDOM%.bat"
copy /y "%~dp0console_host.bat" "%TEMP_HOST%" >nul
if errorlevel 1 (
    echo Could not create the stable Jarvis console host.
    pause
    exit /b 1
)
call "%TEMP_HOST%" "%~dp0"
set "RESULT=%ERRORLEVEL%"
del /q "%TEMP_HOST%" >nul 2>nul
exit /b %RESULT%
