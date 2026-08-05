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
    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return (Get-Content $Path -Raw | ConvertFrom-Json).version
    }
    catch {
        return $null
    }
}

function New-JarvisShortcut {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $ShortcutPath = Join-Path $Desktop "Jarvis.lnk"
    $TargetPath = Join-Path $InstallDir "run_jarvis.bat"

    if (-not (Test-Path $TargetPath)) {
        return
    }

    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $TargetPath
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.Description = "Start Jarvis"
    $Shortcut.Save()
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

    if ($InstalledVersion) {
        Write-Step "Updating Jarvis $InstalledVersion to $RemoteVersion"
    }
    else {
        Write-Step "Installing Jarvis $RemoteVersion"
    }

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
    $Process = Start-Process `
        -FilePath "cmd.exe" `
        -ArgumentList "/d", "/c", "`"$InstallBat`"" `
        -WorkingDirectory $InstallDir `
        -Wait `
        -PassThru

    if ($Process.ExitCode -ne 0) {
        throw "The Python dependency installer failed with exit code $($Process.ExitCode)."
    }

    New-JarvisShortcut

    Write-Host ""
    Write-Host "Jarvis $RemoteVersion is installed in:" -ForegroundColor Green
    Write-Host $InstallDir
    Write-Host ""
    Write-Host "Your .env, config.json, virtual environment, and logs were preserved."
    Write-Host "Use the desktop Jarvis shortcut to start it."
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
