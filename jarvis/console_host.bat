@echo off
setlocal
set "INSTALL_DIR=%~1"
if not defined INSTALL_DIR set "INSTALL_DIR=%~dp0"
cd /d "%INSTALL_DIR%"
title Jarvis Debug Console
set "JARVIS_LAUNCH_MODE=console"

if not exist ".venv\Scripts\python.exe" (
    call install.bat
    if errorlevel 1 exit /b 1
)

:START_JARVIS
".venv\Scripts\python.exe" jarvis.py
set "JARVIS_EXIT=%ERRORLEVEL%"

if "%JARVIS_EXIT%"=="42" (
    echo.
    echo Applying Jarvis update...
    call :WAIT_FOR_UPDATE
    echo.
    echo Restarting Jarvis in this same console...
    echo.
    goto START_JARVIS
)

if "%JARVIS_EXIT%"=="44" (
    echo.
    echo Restarting Jarvis in this same console...
    echo.
    goto START_JARVIS
)

if "%JARVIS_EXIT%"=="43" exit /b 0

echo.
if not "%JARVIS_EXIT%"=="0" (
    echo Jarvis exited with error code %JARVIS_EXIT%.
) else (
    echo Jarvis exited.
)
echo Press any key to close.
pause >nul
exit /b %JARVIS_EXIT%

:WAIT_FOR_UPDATE
set "UPDATE_RESULT=%INSTALL_DIR%\data\update_result.json"
for /l %%I in (1,1,300) do (
    if exist "%UPDATE_RESULT%" goto UPDATE_FINISHED
    >nul 2>nul timeout /t 1 /nobreak
)
echo Update helper did not report completion after 5 minutes.
exit /b 1

:UPDATE_FINISHED
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$p='%UPDATE_RESULT%'; try { $r=Get-Content -Raw $p ^| ConvertFrom-Json; Write-Output ($r.status + ': ' + $r.message) } catch { Write-Output 'Update finished.' }"`) do echo %%S
del /q "%UPDATE_RESULT%" >nul 2>nul
exit /b 0
