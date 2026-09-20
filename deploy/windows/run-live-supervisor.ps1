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

# Phase 26 binds runtime risk/config and portfolio artifacts to the signed
# release manifest. Like Phase 25, it defaults on whenever Phase 11 signed
# release verification is enabled. Set the variable to 0 only for a deliberate
# migration bypass.
$RequireRuntimeProvenanceRaw = [string]$env:FOREX_REQUIRE_RUNTIME_PROVENANCE
if ([string]::IsNullOrWhiteSpace($RequireRuntimeProvenanceRaw)) {
    $RuntimeProvenanceEnabled = $SignedReleaseEnabled
} else {
    $RuntimeProvenanceEnabled = @("1", "true", "yes", "on") -contains $RequireRuntimeProvenanceRaw.ToLowerInvariant()
}
if ($RuntimeProvenanceEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_PROVENANCE requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

# Phase 30 requires an explicit, environment-bound deployment approval after the
# release ceremony. It defaults on whenever signed-release verification is on.
$RequireDeploymentApprovalRaw = [string]$env:FOREX_REQUIRE_DEPLOYMENT_APPROVAL
if ([string]::IsNullOrWhiteSpace($RequireDeploymentApprovalRaw)) {
    $DeploymentApprovalEnabled = $SignedReleaseEnabled
} else {
    $DeploymentApprovalEnabled = @("1", "true", "yes", "on") -contains $RequireDeploymentApprovalRaw.ToLowerInvariant()
}
if ($DeploymentApprovalEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_DEPLOYMENT_APPROVAL requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

$DeploymentEnvironmentId = [string]$env:FOREX_DEPLOYMENT_ENVIRONMENT_ID
if ($DeploymentApprovalEnabled -and [string]::IsNullOrWhiteSpace($DeploymentEnvironmentId)) {
    throw "FOREX_DEPLOYMENT_ENVIRONMENT_ID is required when the deployment approval gate is enabled."
}

$DeploymentApprovalPath = [string]$env:FOREX_DEPLOYMENT_APPROVAL_PATH
if ([string]::IsNullOrWhiteSpace($DeploymentApprovalPath)) {
    $DeploymentApprovalPath = "release/deployment_approval.json"
}

$DeploymentApprovalSignature = [string]$env:FOREX_DEPLOYMENT_APPROVAL_SIGNATURE
if ([string]::IsNullOrWhiteSpace($DeploymentApprovalSignature)) {
    $DeploymentApprovalSignature = "release/deployment_approval.signature.json"
}

$SigningKeyTrustStore = [string]$env:FOREX_SIGNING_KEY_TRUST_STORE
if ([string]::IsNullOrWhiteSpace($SigningKeyTrustStore)) {
    $SigningKeyTrustStore = "release/signing_key_trust.json"
}

$ReleaseReceipt = [string]$env:FOREX_RELEASE_RECEIPT
if ([string]::IsNullOrWhiteSpace($ReleaseReceipt)) {
    $ReleaseReceipt = "release/release_receipt.json"
}

$ReleaseReceiptSignature = [string]$env:FOREX_RELEASE_RECEIPT_SIGNATURE
if ([string]::IsNullOrWhiteSpace($ReleaseReceiptSignature)) {
    $ReleaseReceiptSignature = "release/release_receipt.signature.json"
}

# Phase 31 creates a fresh signed runtime boot receipt immediately before each
# supervised-live start/restart. It defaults on whenever Phase 30 approval is on.
$RequireRuntimeBootRaw = [string]$env:FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION
if ([string]::IsNullOrWhiteSpace($RequireRuntimeBootRaw)) {
    $RuntimeBootEnabled = $DeploymentApprovalEnabled
} else {
    $RuntimeBootEnabled = @("1", "true", "yes", "on") -contains $RequireRuntimeBootRaw.ToLowerInvariant()
}
if ($RuntimeBootEnabled -and -not $DeploymentApprovalEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION requires the Phase 30 deployment approval gate."
}

$RuntimeMachineId = [string]$env:FOREX_RUNTIME_MACHINE_ID
if ($RuntimeBootEnabled -and [string]::IsNullOrWhiteSpace($RuntimeMachineId)) {
    throw "FOREX_RUNTIME_MACHINE_ID is required when runtime boot attestation is enabled."
}

$RuntimeBootPrivateKey = [string]$env:FOREX_RUNTIME_BOOT_PRIVATE_KEY
if ($RuntimeBootEnabled -and [string]::IsNullOrWhiteSpace($RuntimeBootPrivateKey)) {
    throw "FOREX_RUNTIME_BOOT_PRIVATE_KEY is required when runtime boot attestation is enabled."
}

$RuntimeBootReceipt = [string]$env:FOREX_RUNTIME_BOOT_RECEIPT
if ([string]::IsNullOrWhiteSpace($RuntimeBootReceipt)) {
    $RuntimeBootReceipt = "runtime/runtime_boot_receipt.json"
}

$RuntimeBootSignature = [string]$env:FOREX_RUNTIME_BOOT_SIGNATURE
if ([string]::IsNullOrWhiteSpace($RuntimeBootSignature)) {
    $RuntimeBootSignature = "runtime/runtime_boot_receipt.signature.json"
}

# Phase 32 replicates the fresh signed boot evidence to an external append-only
# audit ledger. It defaults on whenever Phase 31 runtime boot attestation is on.
$RequireRemoteAuditRaw = [string]$env:FOREX_REQUIRE_REMOTE_AUDIT_LEDGER
if ([string]::IsNullOrWhiteSpace($RequireRemoteAuditRaw)) {
    $RemoteAuditEnabled = $RuntimeBootEnabled
} else {
    $RemoteAuditEnabled = @("1", "true", "yes", "on") -contains $RequireRemoteAuditRaw.ToLowerInvariant()
}
if ($RemoteAuditEnabled -and -not $RuntimeBootEnabled) {
    throw "FOREX_REQUIRE_REMOTE_AUDIT_LEDGER requires Phase 31 runtime boot attestation."
}

$AuditLedgerRoot = [string]$env:FOREX_AUDIT_LEDGER_ROOT
if ($RemoteAuditEnabled -and [string]::IsNullOrWhiteSpace($AuditLedgerRoot)) {
    throw "FOREX_AUDIT_LEDGER_ROOT is required when remote audit replication is enabled."
}

$AuditLedgerSubdir = [string]$env:FOREX_AUDIT_LEDGER_SUBDIR
if ([string]::IsNullOrWhiteSpace($AuditLedgerSubdir)) {
    $AuditLedgerSubdir = "Forex-trade/audit-ledger"
}

# Phase 33 extends Phase 32 with signed runtime checkpoints during every
# supervised-live cycle. It defaults on whenever the remote audit ledger is on.
$RequireRuntimeLivenessRaw = [string]$env:FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER
if ([string]::IsNullOrWhiteSpace($RequireRuntimeLivenessRaw)) {
    $RuntimeLivenessEnabled = $RemoteAuditEnabled
} else {
    $RuntimeLivenessEnabled = @("1", "true", "yes", "on") -contains $RequireRuntimeLivenessRaw.ToLowerInvariant()
}
if ($RuntimeLivenessEnabled -and -not $RemoteAuditEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER requires the Phase 32 remote audit ledger."
}
if ($RuntimeLivenessEnabled) {
    $env:FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER = "1"
} else {
    $env:FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER = "0"
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

function Test-RuntimeProvenance {
    if (-not $RuntimeProvenanceEnabled) {
        return
    }

    & $Python -m src.runtime_provenance `
        --mode verify `
        --root $ProjectRoot `
        --manifest $Manifest `
        --config config.yaml `
        --reconcile-config $ReconcileConfig `
        --required

    if ($LASTEXITCODE -ne 0) {
        throw "Runtime config/portfolio provenance verification failed. Supervised live will not start."
    }
}

function Test-DeploymentApproval {
    if (-not $DeploymentApprovalEnabled) {
        Write-Warning "Phase 30 deployment approval gate is explicitly disabled by FOREX_REQUIRE_DEPLOYMENT_APPROVAL."
        return
    }

    & $Python -m src.deployment_approval `
        --mode verify `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --approval $DeploymentApprovalPath `
        --signature $DeploymentApprovalSignature `
        --receipt $ReleaseReceipt `
        --receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore

    if ($LASTEXITCODE -ne 0) {
        throw "Deployment approval verification failed. Supervised live will not start."
    }
}

function Write-RuntimeBootAttestation {
    if (-not $RuntimeBootEnabled) {
        Write-Warning "Phase 31 runtime boot attestation is explicitly disabled by FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION."
        return
    }

    & $Python -m src.runtime_boot `
        --mode create `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --machine-id $RuntimeMachineId `
        --private-key $RuntimeBootPrivateKey `
        --receipt $RuntimeBootReceipt `
        --signature $RuntimeBootSignature `
        --approval $DeploymentApprovalPath `
        --approval-signature $DeploymentApprovalSignature `
        --release-receipt $ReleaseReceipt `
        --release-receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore

    if ($LASTEXITCODE -ne 0) {
        throw "Runtime boot attestation creation failed. Supervised live will not start."
    }

    & $Python -m src.runtime_boot `
        --mode verify `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --machine-id $RuntimeMachineId `
        --receipt $RuntimeBootReceipt `
        --signature $RuntimeBootSignature `
        --approval $DeploymentApprovalPath `
        --approval-signature $DeploymentApprovalSignature `
        --release-receipt $ReleaseReceipt `
        --release-receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore `
        --max-age-seconds 300

    if ($LASTEXITCODE -ne 0) {
        throw "Runtime boot attestation verification failed. Supervised live will not start."
    }
}

function Write-RemoteAuditLedger {
    if (-not $RemoteAuditEnabled) {
        Write-Warning "Phase 32 remote audit ledger is explicitly disabled by FOREX_REQUIRE_REMOTE_AUDIT_LEDGER."
        return
    }

    & $Python -m src.audit_ledger `
        --mode append `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --machine-id $RuntimeMachineId `
        --private-key $RuntimeBootPrivateKey `
        --replica-root $AuditLedgerRoot `
        --replica-subdir $AuditLedgerSubdir `
        --boot-receipt $RuntimeBootReceipt `
        --boot-signature $RuntimeBootSignature `
        --approval $DeploymentApprovalPath `
        --approval-signature $DeploymentApprovalSignature `
        --release-receipt $ReleaseReceipt `
        --release-receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore `
        --max-boot-age-seconds 300

    if ($LASTEXITCODE -ne 0) {
        throw "Remote audit ledger append failed. Supervised live will not start."
    }

    & $Python -m src.audit_ledger `
        --mode verify `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --machine-id $RuntimeMachineId `
        --replica-root $AuditLedgerRoot `
        --replica-subdir $AuditLedgerSubdir `
        --trust-store $SigningKeyTrustStore

    if ($LASTEXITCODE -ne 0) {
        throw "Remote audit ledger verification failed. Supervised live will not start."
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
    Test-RuntimeProvenance
    Test-DeploymentApproval
    Test-RestartReconciliation
    Write-RuntimeBootAttestation
    Write-RemoteAuditLedger

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
