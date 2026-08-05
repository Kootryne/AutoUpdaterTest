@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=[regex]::Escape((Resolve-Path '%~dp0').Path); $p=Get-CimInstance Win32_Process | Where-Object {($_.Name -match '^pythonw?\.exe$') -and ($_.CommandLine -match $root) -and ($_.CommandLine -match 'jarvis\.py')}; if($p){$p | ForEach-Object {Stop-Process -Id $_.ProcessId -Force}; Write-Host 'Jarvis stopped.'}else{Write-Host 'Jarvis was not running.'}"
