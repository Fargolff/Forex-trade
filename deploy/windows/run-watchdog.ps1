$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python"
}

# This process is intentionally independent from the trading supervisor.
# It does not connect to MT5, does not read broker credentials and cannot send
# orders. Remote heartbeat/webhook URLs and bearer tokens are environment-only.
& $Python -m src.watchdog --mode daemon
exit $LASTEXITCODE
