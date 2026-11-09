# Build the Windows desktop app:   .\packaging\build.ps1
#
# Produces, in $OutDir (by default a "Wardmind-build" folder in your user folder, outside
# OneDrive and outside this repository):
#   Wardmind\                          the app; double-click Wardmind\Wardmind.exe
#   Wardmind-<version>-windows-x64.zip the same folder, zipped, to copy to another computer
# PyInstaller's working files go to $OutDir\.work and are reused by the next build.

param(
    [string]$OutDir = (Join-Path $env:USERPROFILE "Wardmind-build")
)

# Not "Stop": Windows PowerShell 5.1 would then treat uv's progress lines on stderr as errors.
# Each native command's exit code is checked instead, and each cmdlet stops on its own error.
$repo = Split-Path -Parent $PSScriptRoot
if (-not $env:UV_PROJECT_ENVIRONMENT) {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA "wardmind\venv"
}

$version = (Select-String -Path (Join-Path $repo "pyproject.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$dist = Join-Path $OutDir "dist"
$work = Join-Path $OutDir ".work"
$app = Join-Path $OutDir "Wardmind"
$zip = Join-Path $OutDir "Wardmind-$version-windows-x64.zip"

Push-Location $repo
try {
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    uv run pyinstaller --noconfirm --clean --distpath $dist --workpath $work "packaging\wardmind.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Pop-Location
}

# Replace the previous build only once the new one has succeeded.
if (Test-Path $app) { Remove-Item -Recurse -Force $app -ErrorAction Stop }
Move-Item (Join-Path $dist "Wardmind") $app -ErrorAction Stop
Remove-Item -Recurse -Force $dist -ErrorAction Stop
if (Test-Path $zip) { Remove-Item -Force $zip -ErrorAction Stop }
Compress-Archive -Path $app -DestinationPath $zip -ErrorAction Stop

$size = "{0:N0} MB" -f ((Get-ChildItem -Recurse $app | Measure-Object Length -Sum).Sum / 1MB)
Write-Host ""
Write-Host "Built Wardmind $version ($size):"
Write-Host "  $app\Wardmind.exe"
Write-Host "  $zip"
