@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$startup=[Environment]::GetFolderPath('Startup'); $shell=New-Object -ComObject WScript.Shell; $s=$shell.CreateShortcut((Join-Path $startup 'Jarvis.lnk')); $s.TargetPath="$env:WINDIR\System32\wscript.exe"; $s.Arguments='"%~dp0start_jarvis.vbs"'; $s.WorkingDirectory='%~dp0'; $s.Description='Start Jarvis when Windows signs in'; $s.Save()"
echo Jarvis will start automatically when you sign in.
pause
