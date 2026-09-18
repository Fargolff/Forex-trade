$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python"
}

$ReplicaRoot = [string]$env:FOREX_BACKUP_REPLICA_ROOT
if ([string]::IsNullOrWhiteSpace($ReplicaRoot)) {
    throw "FOREX_BACKUP_REPLICA_ROOT is not set. Scheduled Phase 13 backups require an off-device replica target."
}

$Config = "backup.yaml"
if (-not (Test-Path $Config)) {
    $Config = "backup.example.yaml"
}

$Arguments = @(
    "-m", "src.backup_policy",
    "--mode", "cycle",
    "--root", $ProjectRoot,
    "--config", $Config
)

$ApplyRetention = [string]$env:FOREX_BACKUP_APPLY_RETENTION
$RetentionEnabled = @("1", "true", "yes", "on") -contains $ApplyRetention.ToLowerInvariant()
if ($RetentionEnabled) {
    $PruneAck = [string]$env:FOREX_BACKUP_PRUNE_ACK
    if ($PruneAck -ne "I_UNDERSTAND_BACKUP_PRUNE") {
        throw "Automatic retention deletion requires FOREX_BACKUP_PRUNE_ACK=I_UNDERSTAND_BACKUP_PRUNE."
    }
    $Arguments += @("--apply-retention", "--ack", $PruneAck)
}

& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "Phase 13 backup cycle failed with exit code $LASTEXITCODE"
}
