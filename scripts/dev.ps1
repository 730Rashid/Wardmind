# Dot-source this before working on Wardmind:   . .\scripts\dev.ps1
# Keeps the virtualenv out of OneDrive so it doesn't sync thousands of files.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA "wardmind\venv"
Write-Host "UV_PROJECT_ENVIRONMENT = $env:UV_PROJECT_ENVIRONMENT"
Write-Host "Try: uv sync; uv run pytest -q; uv run wardmind ui"
