@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p=Join-Path ([Environment]::GetFolderPath('Startup')) 'Jarvis.lnk'; if(Test-Path $p){Remove-Item $p -Force; Write-Host 'Automatic startup disabled.'}else{Write-Host 'Automatic startup was already disabled.'}"
pause
