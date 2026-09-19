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
$RequireSignedRelease = [string]$env:FOREX_REQUIRE_SIGNED_RELEASE
$SignedReleaseEnabled = @("1", "true", "yes", "on") -contains $RequireSignedRelease.ToLowerInvariant()

$Manifest = $env:FOREX_RELEASE_MANIFEST
if ([string]::IsNullOrWhiteSpace($Manifest)) {
    $Manifest = "release/release_manifest.json"
}

$ReleaseSignature = $env:FOREX_RELEASE_SIGNATURE
if ([string]::IsNullOrWhiteSpace($ReleaseSignature)) {
    $ReleaseSignature = "release/release_signature.json"
}

$PublicKey = $env:FOREX_RELEASE_PUBLIC_KEY
if ([string]::IsNullOrWhiteSpace($PublicKey)) {
    $PublicKey = "release/forex-release-public.pem"
}

# Phase 12 optionally requires a separately signed deterministic release bundle.
# The bundle gate is intentionally dependent on Phase 11 so a valid bundle alone
# can never replace deployed-file verification.
$RequireSignedBundle = [string]$env:FOREX_REQUIRE_SIGNED_BUNDLE
$SignedBundleEnabled = @("1", "true", "yes", "on") -contains $RequireSignedBundle.ToLowerInvariant()
if ($SignedBundleEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_SIGNED_BUNDLE requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

# Phase 25 binds the external broker calendar to the signed release manifest.
# When signed-release verification is enabled, provenance verification is enabled
# by default as well. If no calendar exists and no binding is present, the gate
# stays neutral unless Phase 24 explicitly requires a calendar.
$CalendarPath = $env:FOREX_MARKET_CALENDAR_PATH
if ([string]::IsNullOrWhiteSpace($CalendarPath)) {
    $CalendarPath = "market_calendar.yaml"
}

$RequireMarketCalendarRaw = [string]$env:FOREX_REQUIRE_MARKET_CALENDAR
$MarketCalendarRequired = @("1", "true", "yes", "on") -contains $RequireMarketCalendarRaw.ToLowerInvariant()

$RequireCalendarProvenanceRaw = [string]$env:FOREX_REQUIRE_MARKET_CALENDAR_PROVENANCE
if ([string]::IsNullOrWhiteSpace($RequireCalendarProvenanceRaw)) {
    $CalendarProvenanceEnabled = $SignedReleaseEnabled
} else {
    $CalendarProvenanceEnabled = @("1", "true", "yes", "on") -contains $RequireCalendarProvenanceRaw.ToLowerInvariant()
}
if ($CalendarProvenanceEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_MARKET_CALENDAR_PROVENANCE requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

$CalendarCoverageRaw = [string]$env:FOREX_MARKET_CALENDAR_MIN_COVERAGE_DAYS
$CalendarMinCoverageDays = 30
if (-not [string]::IsNullOrWhiteSpace($CalendarCoverageRaw)) {
    $ParsedCoverageDays = 0
    if (-not [int]::TryParse($CalendarCoverageRaw, [ref]$ParsedCoverageDays) -or $ParsedCoverageDays -lt 0) {
        throw "FOREX_MARKET_CALENDAR_MIN_COVERAGE_DAYS must be a non-negative integer."
    }
    $CalendarMinCoverageDays = $ParsedCoverageDays
}

# Phase 14 broker/local restart reconciliation is fail-closed by default. A
# temporary migration bypass requires the operator to explicitly set this to 0.
$RestartReconcileRaw = [string]$env:FOREX_REQUIRE_RESTART_RECONCILE
if ([string]::IsNullOrWhiteSpace($RestartReconcileRaw)) {
    $RestartReconcileEnabled = $true
} else {
    $RestartReconcileEnabled = @("1", "true", "yes", "on") -contains $RestartReconcileRaw.ToLowerInvariant()
}

$ReconcileConfig = $env:FOREX_RECONCILE_CONFIG
if ([string]::IsNullOrWhiteSpace($ReconcileConfig)) {
    $ReconcileConfig = "reconcile.yaml"
}

function Test-SignedRelease {
    if (-not $SignedReleaseEnabled) {
        return
    }

    & $Python -m src.release `
        --mode verify `
        --root $ProjectRoot `
        --manifest $Manifest `
        --signature $ReleaseSignature `
        --public-key $PublicKey

    if ($LASTEXITCODE -ne 0) {
        throw "Signed release verification failed. Supervised live will not start."
    }
}

function Test-SignedBundle {
    if (-not $SignedBundleEnabled) {
        return
    }

    $Bundle = $env:FOREX_RELEASE_BUNDLE
    if ([string]::IsNullOrWhiteSpace($Bundle)) {
        $Bundle = "release/forex-release-bundle.zip"
    }

    $BundleSignature = $env:FOREX_RELEASE_BUNDLE_SIGNATURE
    if ([string]::IsNullOrWhiteSpace($BundleSignature)) {
        $BundleSignature = "release/forex-release-bundle.signature.json"
    }

    $ExpectedCommit = $env:FOREX_EXPECTED_RELEASE_COMMIT
    $ExpectedReleaseId = $env:FOREX_EXPECTED_RELEASE_ID
    if ([string]::IsNullOrWhiteSpace($ExpectedCommit) -or [string]::IsNullOrWhiteSpace($ExpectedReleaseId)) {
        throw "Signed bundle gate requires FOREX_EXPECTED_RELEASE_COMMIT and FOREX_EXPECTED_RELEASE_ID."
    }

    & $Python -m src.artifact `
        --mode verify `
        --archive $Bundle `
        --bundle-signature $BundleSignature `
        --public-key $PublicKey `
        --expected-commit $ExpectedCommit `
        --expected-release-id $ExpectedReleaseId `
        --deployed-manifest $Manifest `
        --deployed-release-signature $ReleaseSignature

    if ($LASTEXITCODE -ne 0) {
        throw "Signed release bundle verification failed. Supervised live will not start."
    }
}

function Test-MarketCalendarProvenance {
    if (-not $CalendarProvenanceEnabled) {
        return
    }

    $CalendarArgs = @(
        "-m", "src.calendar_provenance",
        "--mode", "verify",
        "--path", $CalendarPath,
        "--manifest", $Manifest,
        "--min-coverage-days", [string]$CalendarMinCoverageDays
    )
    if ($MarketCalendarRequired) {
        $CalendarArgs += "--required"
    }

    & $Python @CalendarArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Market calendar provenance/freshness verification failed. Supervised live will not start."
    }
}

function Test-RestartReconciliation {
    if (-not $RestartReconcileEnabled) {
        Write-Warning "Phase 14 restart reconciliation is explicitly disabled by FOREX_REQUIRE_RESTART_RECONCILE."
        return
    }

    & $Python -m src.restart_reconcile `
        --mode verify `
        --config config.yaml `
        --reconcile-config $ReconcileConfig

    if ($LASTEXITCODE -ne 0) {
        throw "Broker/local restart reconciliation failed. Supervised live will not start."
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
    Test-SignedBundle
    Test-SignedRelease
    Test-MarketCalendarProvenance
    Test-RestartReconciliation

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
