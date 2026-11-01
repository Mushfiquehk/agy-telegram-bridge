# One-time setup on Windows 11. Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== agy-telegram-bridge setup ==" -ForegroundColor Cyan

# 1. Python
if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = "py"; $pyArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = "python"; $pyArgs = @() }
else { throw "Python 3.10+ not found. Install it: winget install Python.Python.3.12" }
& $pyExe @pyArgs -c "import sys; assert sys.version_info >= (3,10), sys.version" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ required. winget install Python.Python.3.12" }

# 2. agy
$agy = Get-Command agy -ErrorAction SilentlyContinue
if ($agy) { Write-Host "[ok] agy found at $($agy.Source)" -ForegroundColor Green }
else { Write-Warning "agy not on PATH. Install the Antigravity CLI, run 'agy' once to sign in, or set AGY_COMMAND in .env to the full path." }

# 3. venv + install
if (-not (Test-Path ".venv")) { & $pyExe @pyArgs -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Write-Host "[ok] dependencies installed" -ForegroundColor Green

# 4. .env
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env - fill in TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS." -ForegroundColor Yellow
    notepad .env
}

Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\python.exe -m agy_bridge --doctor   # checks agy works headlessly"
Write-Host "  powershell -File scripts\run.ps1                     # start the bridge"
Write-Host "  powershell -File scripts\install-autostart.ps1       # optional: start at logon"
