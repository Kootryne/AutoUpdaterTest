@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo Installing Jarvis MVP
echo ==========================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python launcher "py" was not found.
    echo Install 64-bit Python 3.12 first.
    pause
    exit /b 1
)

echo Checking Windows for OPENAI_API_KEY...
set "OPENAI_KEY_FOUND="

if defined OPENAI_API_KEY (
    set "OPENAI_KEY_FOUND=Process"
)

if not defined OPENAI_KEY_FOUND (
    for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$k=[Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User'); if($k){'User'} elseif([Environment]::GetEnvironmentVariable('OPENAI_API_KEY','Machine')){'Machine'}"`) do (
        set "OPENAI_KEY_FOUND=%%S"
    )
)

if defined OPENAI_KEY_FOUND (
    echo Found OPENAI_API_KEY in the %OPENAI_KEY_FOUND% environment scope.
    echo The key will be used without copying or displaying it.
) else (
    echo OPENAI_API_KEY was not found in Windows.
    echo You will need to add it to .env after installation.
)
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating Python 3.12 virtual environment...
    py -3.12 -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo [1/4] Virtual environment already exists.
)

echo [2/4] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo [3/4] Installing packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [4/4] Creating .env if needed...
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Created .env.
) else (
    echo .env already exists and was not overwritten.
)

echo.
if defined OPENAI_KEY_FOUND (
    echo Jarvis found an existing system API key. No key editing is required.
) else (
    echo Open .env and add OPENAI_API_KEY before starting Jarvis.
)
echo.
echo Installation complete.
echo Double-click run_jarvis.bat when ready.
if not defined JARVIS_NO_PAUSE pause
exit /b 0

:failed
echo.
echo INSTALLATION FAILED. Read the error above.
if not defined JARVIS_NO_PAUSE pause
exit /b 1
