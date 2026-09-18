$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python"
}

# Phase 11 signed-release gate is opt-in until the operator has generated an
# offline signing key and deployed the matching public key. When enabled, every
# supervisor start/restart verifies BOTH the Ed25519 signature and file hashes.
$RequireSignedRelease = $env:FOREX_REQUIRE_SIGNED_RELEASE
$SignedReleaseEnabled = @("1", "true", "yes", "on") -contains $RequireSignedRelease.ToLowerInvariant()

function Test-SignedRelease {
    if (-not $SignedReleaseEnabled) {
        return
    }

    $Manifest = $env:FOREX_RELEASE_MANIFEST
    if ([string]::IsNullOrWhiteSpace($Manifest)) {
        $Manifest = "release/release_manifest.json"
    }

    $Signature = $env:FOREX_RELEASE_SIGNATURE
    if ([string]::IsNullOrWhiteSpace($Signature)) {
        $Signature = "release/release_signature.json"
    }

    $PublicKey = $env:FOREX_RELEASE_PUBLIC_KEY
    if ([string]::IsNullOrWhiteSpace($PublicKey)) {
        $PublicKey = "release/forex-release-public.pem"
    }

    & $Python -m src.release `
        --mode verify `
        --root $ProjectRoot `
        --manifest $Manifest `
        --signature $Signature `
        --public-key $PublicKey

    if ($LASTEXITCODE -ne 0) {
        throw "Signed release verification failed. Supervised live will not start."
    }
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
    Test-SignedRelease

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
