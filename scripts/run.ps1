# Runs the bridge and restarts it if it crashes. Ctrl+C to quit.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Error "Run scripts\setup.ps1 first."; exit 1 }
$env:PYTHONUTF8 = "1"
while ($true) {
    & $python -m agy_bridge @args
    $code = $LASTEXITCODE
    if ($code -eq 2) { Write-Error "Config error - fix .env and rerun."; exit 2 }
    Write-Warning "bridge exited with code $code; restarting in 5s (Ctrl+C to stop)"
    Start-Sleep -Seconds 5
}
