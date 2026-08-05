param(
    [switch]$UpdateOnly,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoOwner = "Kootryne"
$RepoName = "AutoUpdaterTest"
$Branch = "main"
$ProjectFolder = "jarvis"
$InstallDir = Join-Path $env:LOCALAPPDATA "Jarvis"
$ZipUrl = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Branch.zip"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-Version([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json).version }
    catch { return $null }
}

function Stop-JarvisProcess {
    $escaped = [regex]::Escape($InstallDir)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^pythonw?\.exe$' -and
            $_.CommandLine -match $escaped -and
            $_.CommandLine -match 'jarvis\.py'
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Update-EnvDefaults {
    $Path = Join-Path $InstallDir ".env"
    if (-not (Test-Path $Path)) { return }

    $Text = Get-Content $Path -Raw
    $Text = $Text -replace '(?m)^TEXT_MODEL=gpt-5-mini\s*$', 'TEXT_MODEL=gpt-4.1-mini'
    $Text = $Text -replace '(?m)^VAD_AGGRESSIVENESS=2\s*$', 'VAD_AGGRESSIVENESS=3'
    $Text = $Text -replace '(?m)^END_SILENCE_SECONDS=1\.05\s*$', 'END_SILENCE_SECONDS=0.75'
    $Text = $Text -replace '(?m)^FOLLOWUP_END_SILENCE_SECONDS=0\.90\s*$', 'FOLLOWUP_END_SILENCE_SECONDS=0.75'
    $Text = $Text -replace '(?m)^MAX_UTTERANCE_SECONDS=25\s*$', 'MAX_UTTERANCE_SECONDS=12'
    $Text = $Text -replace '(?m)^MAX_HISTORY_MESSAGES=12\s*$', 'MAX_HISTORY_MESSAGES=8'

    $Missing = @{
        'FOLLOWUP_MODEL' = 'gpt-4.1-nano'
        'VAD_WINDOW_FRAMES' = '10'
        'VAD_MIN_VOICED_FRAMES' = '4'
        'FOLLOWUP_START_WINDOW_FRAMES' = '8'
        'FOLLOWUP_START_MIN_VOICED_FRAMES' = '3'
        'HARD_MAX_UTTERANCE_SECONDS' = '12'
        'AUTO_UPDATE_ENABLED' = 'true'
        'UPDATE_CHECK_INTERVAL_SECONDS' = '3600'
        'UPDATE_MANIFEST_URL' = 'https://raw.githubusercontent.com/Kootryne/AutoUpdaterTest/main/jarvis/manifest.json'
        'UPDATE_SOURCE_ZIP_URL' = 'https://github.com/Kootryne/AutoUpdaterTest/archive/refs/heads/main.zip'
    }
    foreach ($Key in $Missing.Keys) {
        if ($Text -notmatch "(?m)^$([regex]::Escape($Key))=") {
            $Text += "`r`n$Key=$($Missing[$Key])"
        }
    }
    Set-Content -Path $Path -Value $Text.TrimEnd() -Encoding UTF8
}

function New-Shortcut(
    [string]$ShortcutPath,
    [string]$TargetPath,
    [string]$Arguments,
    [string]$Description,
    [string]$WorkingDirectory
) {
    $Parent = Split-Path $ShortcutPath -Parent
    if ($Parent) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $TargetPath
    $Shortcut.Arguments = $Arguments
    $Shortcut.WorkingDirectory = $WorkingDirectory
    $Shortcut.Description = $Description
    $Shortcut.Save()
}

function Install-JarvisShortcuts {
    $Wscript = Join-Path $env:WINDIR "System32\wscript.exe"
    $HiddenScript = Join-Path $InstallDir "start_jarvis.vbs"
    $QuotedScript = '"' + $HiddenScript + '"'

    $Desktop = [Environment]::GetFolderPath("Desktop")
    New-Shortcut `
        (Join-Path $Desktop "Jarvis.lnk") `
        $Wscript `
        $QuotedScript `
        "Start Jarvis" `
        $InstallDir

    $Programs = [Environment]::GetFolderPath("Programs")
    $StartMenu = Join-Path $Programs "Jarvis"
    New-Shortcut `
        (Join-Path $StartMenu "Start Jarvis.lnk") `
        $Wscript `
        $QuotedScript `
        "Start Jarvis" `
        $InstallDir
    New-Shortcut `
        (Join-Path $StartMenu "Jarvis Debug Console.lnk") `
        (Join-Path $InstallDir "run_jarvis.bat") `
        "" `
        "Start Jarvis with visible debug logs" `
        $InstallDir
    New-Shortcut `
        (Join-Path $StartMenu "Stop Jarvis.lnk") `
        (Join-Path $InstallDir "stop_jarvis.bat") `
        "" `
        "Stop Jarvis" `
        $InstallDir

    $Startup = [Environment]::GetFolderPath("Startup")
    New-Shortcut `
        (Join-Path $Startup "Jarvis.lnk") `
        $Wscript `
        $QuotedScript `
        "Start Jarvis when Windows signs in" `
        $InstallDir
}

function Test-OpenAIKey {
    if ($env:OPENAI_API_KEY) { return $true }
    if ([Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')) { return $true }
    if ([Environment]::GetEnvironmentVariable('OPENAI_API_KEY','Machine')) { return $true }
    $EnvPath = Join-Path $InstallDir '.env'
    if (Test-Path $EnvPath) {
        $Line = Get-Content $EnvPath | Where-Object { $_ -match '^OPENAI_API_KEY=.+$' } | Select-Object -First 1
        if ($Line) { return $true }
    }
    return $false
}

$TempRoot = Join-Path $env:TEMP ("jarvis_setup_" + [Guid]::NewGuid().ToString("N"))
$ZipPath = Join-Path $TempRoot "source.zip"
$ExtractPath = Join-Path $TempRoot "source"

try {
    New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
    Write-Step "Downloading the latest Jarvis files from GitHub"
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing

    Write-Step "Extracting download"
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractPath -Force

    $SourceDir = Join-Path $ExtractPath "$RepoName-$Branch\$ProjectFolder"
    $ManifestPath = Join-Path $SourceDir "manifest.json"
    if (-not (Test-Path $ManifestPath)) {
        throw "The downloaded repository does not contain $ProjectFolder\manifest.json."
    }

    $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    $RemoteVersion = [string]$Manifest.version
    $InstalledVersion = Get-Version (Join-Path $InstallDir "version.json")

    if ($UpdateOnly -and -not $Force -and $InstalledVersion -eq $RemoteVersion) {
        Write-Host ""
        Write-Host "Jarvis is already up to date: version $RemoteVersion" -ForegroundColor Green
        exit 0
    }

    Stop-JarvisProcess
    if ($InstalledVersion) { Write-Step "Updating Jarvis $InstalledVersion to $RemoteVersion" }
    else { Write-Step "Installing Jarvis $RemoteVersion" }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    foreach ($RelativePath in $Manifest.managed_files) {
        $SourceFile = Join-Path $SourceDir $RelativePath
        $DestinationFile = Join-Path $InstallDir $RelativePath
        if (-not (Test-Path $SourceFile)) {
            throw "Managed file is missing from GitHub package: $RelativePath"
        }
        $DestinationParent = Split-Path $DestinationFile -Parent
        if ($DestinationParent) {
            New-Item -ItemType Directory -Path $DestinationParent -Force | Out-Null
        }
        Copy-Item -Path $SourceFile -Destination $DestinationFile -Force
    }

    foreach ($RelativePath in $Manifest.copy_if_missing) {
        $SourceFile = Join-Path $SourceDir $RelativePath
        $DestinationFile = Join-Path $InstallDir $RelativePath
        if (-not (Test-Path $DestinationFile)) {
            Copy-Item -Path $SourceFile -Destination $DestinationFile -Force
        }
    }

    Write-Step "Installing or updating Python dependencies"
    $env:JARVIS_NO_PAUSE = "1"
    $InstallBat = Join-Path $InstallDir "install.bat"
    $Process = Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", "`"$InstallBat`"" -WorkingDirectory $InstallDir -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "The Python dependency installer failed with exit code $($Process.ExitCode)."
    }

    Update-EnvDefaults
    Install-JarvisShortcuts

    Write-Host ""
    Write-Host "Jarvis $RemoteVersion is installed in:" -ForegroundColor Green
    Write-Host $InstallDir
    Write-Host "Jarvis will start automatically when you sign in."
    Write-Host "Your .env, config.json, virtual environment, and logs were preserved."

    if (Test-OpenAIKey) {
        Start-Process -FilePath (Join-Path $env:WINDIR "System32\wscript.exe") -ArgumentList ('"' + (Join-Path $InstallDir 'start_jarvis.vbs') + '"')
        Write-Host "Jarvis has been started in the background."
    }
    else {
        Write-Host "Add OPENAI_API_KEY, then use the desktop Jarvis shortcut."
    }
}
catch {
    Write-Host ""
    Write-Host "INSTALLATION OR UPDATE FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path $TempRoot) {
        Remove-Item $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
