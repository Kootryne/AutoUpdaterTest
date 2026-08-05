param()

$ErrorActionPreference = "Stop"
$JarvisDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvPath = Join-Path $JarvisDir ".env"
$TokenUrl = "https://github.com/settings/personal-access-tokens/new"

Write-Host ""
Write-Host "Jarvis GitHub connection" -ForegroundColor Cyan
Write-Host ""
Write-Host "The token should be restricted to Kootryne/AutoUpdaterTest with:"
Write-Host "  Contents: Read and write"
Write-Host "  Issues: Read and write"
Write-Host ""
Write-Host "Opening GitHub's fine-grained token page..."
Start-Process $TokenUrl

$SecureToken = Read-Host "Paste the token here (input is hidden)" -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try {
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}

if (-not $Token -or $Token.Length -lt 20) {
    throw "No valid-looking token was entered."
}

$Lines = @()
if (Test-Path $EnvPath) {
    $Lines = Get-Content $EnvPath
}

$Lines = @(
    $Lines | Where-Object {
        $_ -notmatch '^\s*(GITHUB_TOKEN|GH_TOKEN|JARVIS_GITHUB_REPOSITORY|JARVIS_GITHUB_BRANCH)\s*='
    }
)
$Lines += "GITHUB_TOKEN=$Token"
$Lines += "JARVIS_GITHUB_REPOSITORY=Kootryne/AutoUpdaterTest"
$Lines += "JARVIS_GITHUB_BRANCH=main"

[IO.File]::WriteAllLines($EnvPath, $Lines, [Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "GitHub is connected for Jarvis." -ForegroundColor Green
Write-Host "Restart Jarvis before posting skills or suggestions."
