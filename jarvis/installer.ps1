param([switch]$UpdateOnly,[switch]$Force)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$Owner='Kootryne'; $Repo='AutoUpdaterTest'; $Branch='main'; $Folder='jarvis'
$InstallDir=Join-Path $env:LOCALAPPDATA 'Jarvis'
$ZipUrl="https://github.com/$Owner/$Repo/archive/refs/heads/$Branch.zip"

function Step([string]$Text){Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan}
function Version([string]$Path){try{(Get-Content $Path -Raw|ConvertFrom-Json).version}catch{$null}}
function Stop-Jarvis{
  $escaped=[regex]::Escape($InstallDir)
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {$_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $escaped -and $_.CommandLine -match 'jarvis\.py'} |
    ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
}
function Set-Line([string]$Path,[string]$Name,[string]$Value){
  if(!(Test-Path $Path)){return}
  $text=Get-Content $Path -Raw
  if($text -match "(?m)^$([regex]::Escape($Name))="){$text=[regex]::Replace($text,"(?m)^$([regex]::Escape($Name))=.*$","$Name=$Value")}
  else{$text=$text.TrimEnd()+"`r`n$Name=$Value`r`n"}
  Set-Content $Path $text -Encoding UTF8
}
function Patch-Code{
  $audio=Join-Path $InstallDir 'jarvis_app\audio.py'
  if(Test-Path $audio){
    $t=Get-Content $audio -Raw
    $t=$t.Replace('return vad_result or rms >= self.settings.energy_threshold','return vad_result and rms >= self.settings.energy_threshold')
    Set-Content $audio $t -Encoding UTF8
  }
  $brain=Join-Path $InstallDir 'jarvis_app\brain.py'
  if(Test-Path $brain){
    $t=Get-Content $brain -Raw
    $t=$t.Replace('You are Jarvis, a fast voice assistant on {owner}''s computer.','You are Jarvis, a fast voice assistant on {owner}''s computer.`nThe user speaks through a microphone and you receive a transcript. Never claim you only receive typed messages.')
    Set-Content $brain $t -Encoding UTF8
  }
}
function Shortcut([string]$Path,[string]$Target,[string]$Args,[string]$Description){
  $parent=Split-Path $Path -Parent; if($parent){New-Item -ItemType Directory $parent -Force|Out-Null}
  $shell=New-Object -ComObject WScript.Shell; $s=$shell.CreateShortcut($Path)
  $s.TargetPath=$Target; $s.Arguments=$Args; $s.WorkingDirectory=$InstallDir; $s.Description=$Description; $s.Save()
}
function Install-Shortcuts{
  $wscript=Join-Path $env:WINDIR 'System32\wscript.exe'
  $hidden=Join-Path $InstallDir 'start_jarvis.vbs'; $args='"'+$hidden+'"'
  Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jarvis.lnk') $wscript $args 'Start Jarvis'
  $menu=Join-Path ([Environment]::GetFolderPath('Programs')) 'Jarvis'
  Shortcut (Join-Path $menu 'Start Jarvis.lnk') $wscript $args 'Start Jarvis'
  Shortcut (Join-Path $menu 'Jarvis Debug Console.lnk') (Join-Path $InstallDir 'run_jarvis.bat') '' 'Start Jarvis with visible logs'
  Shortcut (Join-Path $menu 'Stop Jarvis.lnk') (Join-Path $InstallDir 'stop_jarvis.bat') '' 'Stop Jarvis'
  Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'Jarvis.lnk') $wscript $args 'Start Jarvis when Windows signs in'
}
function Has-Key{
  if($env:OPENAI_API_KEY){return $true}
  if([Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')){return $true}
  if([Environment]::GetEnvironmentVariable('OPENAI_API_KEY','Machine')){return $true}
  $p=Join-Path $InstallDir '.env'; if(Test-Path $p){return [bool](Get-Content $p|Where-Object{$_ -match '^OPENAI_API_KEY=.+$'}|Select-Object -First 1)}
  return $false
}

$tmp=Join-Path $env:TEMP ('jarvis_'+[guid]::NewGuid().ToString('N'))
try{
  New-Item -ItemType Directory $tmp -Force|Out-Null
  Step 'Downloading Jarvis from GitHub'; $zip=Join-Path $tmp 'source.zip'; Invoke-WebRequest $ZipUrl -OutFile $zip -UseBasicParsing
  Step 'Extracting'; $extract=Join-Path $tmp 'source'; Expand-Archive $zip $extract -Force
  $src=Join-Path $extract "$Repo-$Branch\$Folder"; $manifest=Get-Content (Join-Path $src 'manifest.json') -Raw|ConvertFrom-Json
  $remote=[string]$manifest.version; $installed=Version (Join-Path $InstallDir 'version.json')
  if($UpdateOnly -and !$Force -and $installed -eq $remote){Write-Host "Jarvis is already up to date: $remote" -ForegroundColor Green; exit 0}
  Stop-Jarvis; Step "Installing Jarvis $remote"; New-Item -ItemType Directory $InstallDir -Force|Out-Null
  foreach($rel in $manifest.managed_files){$from=Join-Path $src $rel; $to=Join-Path $InstallDir $rel; New-Item -ItemType Directory (Split-Path $to -Parent) -Force|Out-Null; Copy-Item $from $to -Force}
  foreach($rel in $manifest.copy_if_missing){$to=Join-Path $InstallDir $rel; if(!(Test-Path $to)){Copy-Item (Join-Path $src $rel) $to -Force}}
  Step 'Installing dependencies'; $env:JARVIS_NO_PAUSE='1'; $p=Start-Process cmd.exe -ArgumentList '/d','/c',('"'+(Join-Path $InstallDir 'install.bat')+'"') -WorkingDirectory $InstallDir -Wait -PassThru; if($p.ExitCode){throw "Dependency installer failed: $($p.ExitCode)"}
  $envfile=Join-Path $InstallDir '.env'
  Set-Line $envfile 'TEXT_MODEL' 'gpt-4.1-mini'; Set-Line $envfile 'VAD_AGGRESSIVENESS' '3'; Set-Line $envfile 'END_SILENCE_SECONDS' '0.75'; Set-Line $envfile 'FOLLOWUP_END_SILENCE_SECONDS' '0.75'; Set-Line $envfile 'MAX_UTTERANCE_SECONDS' '12'; Set-Line $envfile 'MAX_HISTORY_MESSAGES' '8'
  Patch-Code; Install-Shortcuts
  Write-Host ''; Write-Host "Jarvis $remote installed. It will start automatically when you sign in." -ForegroundColor Green
  if(Has-Key){Start-Process (Join-Path $env:WINDIR 'System32\wscript.exe') -ArgumentList ('"'+(Join-Path $InstallDir 'start_jarvis.vbs')+'"'); Write-Host 'Jarvis started in the background.'}
  else{Write-Host 'Add OPENAI_API_KEY, then use the desktop Jarvis shortcut.'}
}catch{Write-Host ''; Write-Host 'INSTALLATION OR UPDATE FAILED' -ForegroundColor Red; Write-Host $_.Exception.Message -ForegroundColor Red; exit 1}
finally{if(Test-Path $tmp){Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue}}
