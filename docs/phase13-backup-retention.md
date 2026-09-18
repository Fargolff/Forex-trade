# Phase 13 — Backup Retention & Off-device Replication

Phase 13 adds a managed backup lifecycle around the Phase 10 runtime backup format.

The goal is to protect the trading runtime against disk loss, accidental deletion, machine failure and local backup corruption without adding cloud credentials to the repository.

## Security and safety model

- Backup creation still uses the Phase 10 ZIP manifest with SHA-256 per file.
- Every newly created backup is verified immediately.
- Off-device copies are written through a temporary file and atomically renamed only after copying completes.
- The replica is re-opened, structurally verified and compared byte-for-byte by SHA-256 against the local archive.
- A local archive is not eligible for pruning unless a verified replica with the same SHA-256 is still reachable at prune time.
- Destructive pruning requires the explicit acknowledgement `I_UNDERSTAND_BACKUP_PRUNE`.
- The Windows scheduled task does not enable retention deletion unless the operator explicitly enables it through environment variables.
- The off-device destination path is provided by environment variable and is not stored in Git.
- Phase 13 does not contain MT5 order execution code.

## Files

```text
backup.example.yaml
src/backup_policy.py
deploy/windows/run-backup-cycle.ps1
deploy/windows/install-backup-task.ps1
runtime/backup_catalog.json
runtime/backup_restore_drill.json
backups/runtime/*.zip
```

`backup.yaml` is intended for local settings and is gitignored.

## 1. Configure the backup policy

Copy the example:

```powershell
Copy-Item backup.example.yaml backup.yaml
```

Default retention buckets are:

```text
keep_latest: 7
keep_daily: 14
keep_weekly: 8
keep_monthly: 12
```

A backup is retained when it belongs to any bucket. The policy is therefore conservative: recent backups are dense while older backups are thinned to weekly/monthly restore points.

## 2. Choose an off-device target

Set the root through an environment variable:

```powershell
setx FOREX_BACKUP_REPLICA_ROOT "Z:\ForexBackups"
```

Suitable targets include:

- an encrypted external drive;
- a NAS/UNC/network share;
- a folder managed by an approved encrypted backup/sync product;
- another machine with appropriate filesystem permissions.

Phase 13 intentionally does not store provider API keys, cloud passwords or storage credentials.

## 3. Run one managed backup cycle

```powershell
python -m src.backup_policy `
  --mode cycle `
  --root . `
  --config backup.yaml
```

The cycle performs:

```text
Create local backup
      ↓
Verify local backup
      ↓
Record SHA-256 in atomic catalog
      ↓
Copy to off-device target via temporary file
      ↓
Verify replica archive
      ↓
Compare replica SHA-256 with local archive
      ↓
Calculate retention plan
```

By default, the retention plan does not delete anything.

## 4. Verify the managed catalog

```powershell
python -m src.backup_policy --mode verify --config backup.yaml
```

This re-checks local archives and all catalogued replicas that are still present.

## 5. Review retention before deletion

```powershell
python -m src.backup_policy --mode retention-plan --config backup.yaml
python -m src.backup_policy --mode prune --config backup.yaml
```

The second command is still dry-run mode. It reports:

- retained backups;
- backups eligible for deletion;
- backups blocked from deletion because a verified replica is unavailable.

## 6. Apply retention explicitly

Only after reviewing the plan:

```powershell
python -m src.backup_policy `
  --mode prune `
  --config backup.yaml `
  --apply `
  --ack I_UNDERSTAND_BACKUP_PRUNE
```

Before each deletion, Phase 13 re-checks that a byte-identical verified replica is still reachable when `require_verified_replica_before_prune: true`.

## 7. Restore drill

A backup is useful only if it can be restored.

Run a non-production restore drill:

```powershell
python -m src.backup_policy `
  --mode restore-drill `
  --config backup.yaml
```

The drill:

1. verifies the backup archive;
2. restores it into a temporary directory;
3. re-checks size and SHA-256 of every restored file;
4. deletes the temporary drill directory automatically;
5. writes the result to `runtime/backup_restore_drill.json`.

It never writes over the live runtime tree.

## 8. Windows scheduled backup

Install a daily task, defaulting to 02:15 local Windows time:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/windows/install-backup-task.ps1
```

Custom schedule example:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/windows/install-backup-task.ps1 -Hour 3 -Minute 30
```

The scheduled runner refuses to run if `FOREX_BACKUP_REPLICA_ROOT` is missing. This prevents a machine from silently accumulating only local backups while the operator assumes off-device protection exists.

### Optional automated retention deletion

Scheduled pruning remains disabled unless both variables are explicitly set:

```powershell
setx FOREX_BACKUP_APPLY_RETENTION "1"
setx FOREX_BACKUP_PRUNE_ACK "I_UNDERSTAND_BACKUP_PRUNE"
```

Even then, the verified-replica rule still applies before every local deletion.

## Failure behavior

Examples:

```text
Local archive corrupt
  → cycle fails before replication

Replica copy interrupted
  → temporary replica file is never promoted

Replica corrupt / different SHA
  → replication fails
  → local backup remains

Replica drive/share unavailable during prune
  → local backup is not eligible for deletion

Restore drill mismatch
  → report ok=false
  → no production files are touched
```

## Limitations

- Phase 13 provides filesystem replication rather than a provider-specific cloud API.
- Encryption-at-rest depends on the chosen storage target; use an encrypted drive/share or approved encrypted backup product when backups contain sensitive configuration.
- A single replica target is not equivalent to a geographically independent disaster-recovery architecture.
- Retention policy protects backup files, not broker-side positions. Broker reconciliation remains a separate operational control.
- Automated restore into the live trading directory is intentionally not provided.
