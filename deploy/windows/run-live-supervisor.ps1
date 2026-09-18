$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python"
}

# Safety: this script never enables live trading and never stores the arming
# phrase. Set FOREX_LIVE_ARM_PHRASE in the Windows user environment only after
# config.yaml has been intentionally reviewed and live.enabled=true.
$ArmPhrase = $env:FOREX_LIVE_ARM_PHRASE
if ([string]::IsNullOrWhiteSpace($ArmPhrase)) {
    throw "FOREX_LIVE_ARM_PHRASE is not set; supervised live will not start."
}

$MaxRestarts = 5
$RestartDelaySeconds = 15
$Restarts = 0

while ($Restarts -le $MaxRestarts) {
    & $Python -m src.production --mode supervised-live --arm-live $ArmPhrase
    $ExitCode = $LASTEXITCODE

    if ($ExitCode -eq 0) {
        exit 0
    }

    $Restarts += 1
    if ($Restarts -gt $MaxRestarts) {
        Write-Error "Supervisor restart budget exhausted. Manual review required."
        exit $ExitCode
    }

    Start-Sleep -Seconds $RestartDelaySeconds
}
