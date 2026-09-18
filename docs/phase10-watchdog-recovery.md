# Phase 10 — External Watchdog & Disaster Recovery

Phase 10 adds controls that remain useful even when the trading Python process, MT5 terminal, or entire trading PC stops responding. It does not relax any Phase 7–9 live-order gate.

## Architecture

```text
Trading PC
  MT5 + Phase 9 production supervisor
            |
            | runtime/live_heartbeat.json
            v
Independent watchdog process / second PC
  src.watchdog
            |
            +--> local alert outbox
            +--> optional webhook alert

Disaster recovery
  src.recovery
    +--> SHA-256 backup manifest
    +--> verify before restore
    +--> preview restore by default
    +--> explicit acknowledgement for in-place restore

Operational soak
  src.soak
    +--> deterministic heartbeat outage
    +--> degraded-state period
    +--> clock-jump incident
    +--> recovery assertions
```

## 1. Independent watchdog

Copy the template:

```powershell
Copy-Item watchdog.example.yaml watchdog.yaml
```

One read-only check:

```powershell
python -m src.watchdog --mode once
```

Long-running watchdog:

```powershell
python -m src.watchdog --mode daemon
```

Exit codes are designed for external monitoring:

- `0` = OK
- `2` = WARN
- `3` = CRITICAL

The watchdog never connects to MT5 and has no order path.

### Local/shared-file source

By default it reads:

```text
runtime/live_heartbeat.json
```

A second Windows machine can point `heartbeat_path` to a UNC/network-share path. Ensure the second machine has read-only access if possible.

### Remote HTTP source

If `FOREX_HEARTBEAT_URL` is defined, the watchdog reads heartbeat JSON from that URL instead of the local file. Optional bearer authentication uses:

```text
FOREX_HEARTBEAT_BEARER
```

Do not put heartbeat tokens or webhook secrets in YAML or Git.

### Alerts

Watchdog alert delivery is configured only through environment variables:

```text
FOREX_WATCHDOG_WEBHOOK_URL
FOREX_WATCHDOG_WEBHOOK_BEARER   # optional
```

Every alert is also written to `runtime/watchdog_alerts.jsonl` and cooldown-deduplicated.

The watchdog reports CRITICAL for:

- missing/unreadable heartbeat
- heartbeat timeout
- heartbeat timestamp materially ahead of watchdog UTC
- remote heartbeat status `HALTED` / `CRITICAL`
- critical incidents embedded in the heartbeat
- accessible Phase 9 `production_halt.json`

A remote `DEGRADED` heartbeat is WARN, not an automatic CRITICAL.

## 2. Disaster-recovery backup

Create a backup:

```powershell
python -m src.recovery --mode backup
```

The archive contains existing selected runtime/config/portfolio files plus `backup_manifest.json` with SHA-256 and byte size for each file.

Environment-only secrets are not included.

Verify an archive before any restore:

```powershell
python -m src.recovery --mode verify-backup --archive backups/runtime-YYYYMMDD-HHMMSS.zip
```

### Preview restore — recommended first

```powershell
python -m src.recovery --mode restore-preview `
  --archive backups/runtime-YYYYMMDD-HHMMSS.zip `
  --target runtime/restore-preview
```

Inspect the restored files manually before replacing production runtime state.

### In-place restore

In-place restore is intentionally gated:

```powershell
python -m src.recovery --mode restore-in-place `
  --archive backups/runtime-YYYYMMDD-HHMMSS.zip `
  --ack I_UNDERSTAND_RUNTIME_RESTORE
```

Before an in-place restore:

1. stop the live supervisor and watchdog task
2. verify actual broker positions directly in MT5
3. verify the backup archive
4. make a fresh backup of current broken/suspect state for forensics
5. restore
6. keep `live.enabled: false` until state and broker positions are reconciled
7. run Phase 8/9 health checks before any armed live cycle

Restore does not set the live arming environment variable.

## 3. Deployment integrity manifest

Generate hashes after installing a reviewed deployment:

```powershell
python -m src.recovery --mode make-manifest
```

Default output:

```text
runtime/deployment_manifest.json
```

Verify later:

```powershell
python -m src.recovery --mode verify-manifest
```

The manifest covers Python source, requirements, example configs and Windows deployment scripts. A mismatch is an operator-review event. SHA-256 detects drift/corruption but is not a substitute for signed releases or protected deployment credentials.

## 4. Soak harness

Run deterministic operational failure simulation:

```powershell
python -m src.soak --cycles 2000
```

The harness must prove that:

- an extended missing-heartbeat window becomes `HEARTBEAT_TIMEOUT`
- the watchdog returns to OK after heartbeat recovery
- a degraded remote status produces WARN rather than false CRITICAL
- a clock-ahead jump is detected
- a normal heartbeat after the clock incident recovers to OK

This is a CI operational state-machine test. It does not replace a real multi-day broker soak on the user's MT5/broker/network.

## 5. Windows watchdog supervision

Templates:

```text
deploy/windows/run-watchdog.ps1
deploy/windows/install-watchdog-task.ps1
```

Install on the watchdog machine:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/windows/install-watchdog-task.ps1
```

For meaningful protection against a complete trading-PC failure, run the watchdog task on a physically/logically separate machine and use a remote/shared heartbeat source.

## 6. Disaster-recovery drill cadence

A backup that has never been restored is not proven recoverable. Periodically perform a non-production drill:

1. create a fresh backup
2. verify it
3. restore into a preview directory on another machine/folder
4. compare hashes and state files
5. run `verify-manifest`
6. run the soak harness
7. document the drill date/result

Do not perform a production in-place restore merely as a routine test while live trading is armed.

## Remaining limits

Phase 10 still does not provide:

- cryptographic release signing with an offline/private signing key
- a hosted heartbeat transport/server
- broker-specific weekend/holiday market-calendar semantics
- automatic reconciliation of restored local state against every broker-side edge case
- evidence that any strategy is profitable

The safe response to uncertainty remains fail-closed and operator review.
